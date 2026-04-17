"""Telegram bot for ATS monitoring and alerting.

Requires env vars:
  TELEGRAM_TOKEN    — Bot API token from @BotFather
  TELEGRAM_CHAT_ID  — Chat/channel ID to send alerts to

Run: python dashboard/telegram_bot.py

Commands:
  /status    — system alive/killed, equity, drawdown
  /positions — open positions per symbol
  /stats     — signal count, total P&L estimate

Alerts (auto-push every 10s diff):
  KILL SWITCH activated
  Trade opened / closed
  Drawdown > 5% warning
  Heartbeat lost > 45s
  Daily PnL summary at 22:00 UTC
  Drift detection triggered
  Model swap (new model accepted or rejected)
"""
import asyncio
import logging
import os
from pathlib import Path
try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None
import sys
import time
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config import EOD_HOUR_GMT, DAILY_LOSS_LIMIT_PCT, MAX_DRAWDOWN_PCT, PROFIT_TARGET_PCT
from dashboard.state_reader import read_state, LiveState, SymbolState

def read_ftmo_state():
    return None

from telegram import Update
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes

logger = logging.getLogger("telegram_bot")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

dotenv_path = Path(__file__).parent.parent / ".env"
if load_dotenv and dotenv_path.exists():
    load_dotenv(dotenv_path)

TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
POLL_INTERVAL        = 10               # seconds between state diffs
HB_WARN_SEC          = 45               # warn if heartbeat older than this
EOD_SUMMARY_HOUR_UTC = EOD_HOUR_GMT     # send daily P&L summary at EOD hour

# Path to signal_server.log for model-swap / drift detection scanning
_LOG_PATH = os.path.join(os.path.dirname(__file__), "..", "logs", "signal_server.log")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_pnl(pnl: float) -> str:
    sign = "+" if pnl >= 0 else ""
    return f"{sign}${pnl:.2f}"


def _md(text: str) -> str:
    """Escape plain text for MarkdownV2."""
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


def _age_seconds(iso_ts: str) -> float:
    """Seconds since an ISO-8601 UTC timestamp."""
    if not iso_ts:
        return float("inf")
    try:
        ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except ValueError:
        return float("inf")


# ---------------------------------------------------------------------------
# Alert diffing
# ---------------------------------------------------------------------------

def _tail_log(path: str, lines: int = 200) -> list[str]:
    """Return last `lines` lines of a log file (best-effort)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            chunk = min(size, lines * 150)
            f.seek(max(0, size - chunk))
            data = f.read()
        return data.decode("utf-8", errors="replace").splitlines()[-lines:]
    except Exception:
        return []


class AlertDiffer:
    """Track previous state to detect changes worth alerting."""

    def __init__(self):
        self._prev: LiveState | None = None
        self._hb_warned = False                   # suppress repeated HB warnings
        self._last_summary_day: int = -1          # day-of-year for daily summary
        self._equity_at_session_open: float = 0.0 # for daily P&L calc
        self._last_log_size: int = 0              # for log scanning (drift/swap)
        self._ftmo_daily_warned: bool = False     # suppress repeated daily DD warnings
        self._ftmo_total_warned: bool = False     # suppress repeated total DD warnings
        self._ftmo_prev_failed: bool = False      # track phase-fail state transitions
        self._ftmo_prev_passed: bool = False      # track phase-pass state transitions

    def diff(self, cur: LiveState) -> list[str]:
        """Return list of alert message strings (may be empty)."""
        if self._prev is None:
            self._prev = cur
            self._equity_at_session_open = cur.equity
            return []

        alerts: list[str] = []
        prev = self._prev
        now = datetime.now(timezone.utc)

        # ── Kill switch transition ──────────────────────────────────────────
        if cur.is_killed and not prev.is_killed:
            reason = cur.system.get("kill_reason", "")
            alerts.append(
                "\U0001f534 *KILL SWITCH* activated\\!\n"
                f"Drawdown: {_md(f'{cur.drawdown_pct:.1f}%')}"
                + (f"\nReason: {_md(reason)}" if reason else "")
            )

        # ── Drawdown warning (crosses half of daily loss limit) ─────────────
        if cur.drawdown_pct >= DAILY_LOSS_LIMIT_PCT * 0.5 and prev.drawdown_pct < DAILY_LOSS_LIMIT_PCT * 0.5:
            alerts.append(
                f"\u26a0\ufe0f Daily drawdown {_md(f'{cur.drawdown_pct:.1f}%')} "
                f"\u2014 approaching kill limit"
            )

        # ── FTMO-specific alerts ─────────────────────────────────────────────
        ftmo = read_ftmo_state()
        if ftmo:
            phase = ftmo.get("phase", {})
            daily_dd   = float(phase.get("daily_loss_pct", 0.0))
            total_dd   = float(phase.get("total_dd_pct", 0.0))
            max_daily  = float(phase.get("max_daily_loss_pct", DAILY_LOSS_LIMIT_PCT))
            max_total  = float(phase.get("max_total_loss_pct", MAX_DRAWDOWN_PCT))
            # Warn at 80% of each FTMO limit
            daily_warn_thresh = max_daily * 0.80
            total_warn_thresh = max_total * 0.80
            if daily_dd >= daily_warn_thresh and not self._ftmo_daily_warned:
                alerts.append(
                    f"\u26a0\ufe0f *FTMO Daily DD* {_md(f'{daily_dd:.2f}%')} "
                    f"/ limit {_md(f'{max_daily:.1f}%')} "
                    f"\\({_md(f'{daily_dd/max_daily*100:.0f}%')} of limit\\)"
                )
                self._ftmo_daily_warned = True
            elif daily_dd < daily_warn_thresh * 0.5:
                self._ftmo_daily_warned = False   # reset once DD recovers

            if total_dd >= total_warn_thresh and not self._ftmo_total_warned:
                alerts.append(
                    f"\U0001f534 *FTMO Total DD* {_md(f'{total_dd:.2f}%')} "
                    f"/ limit {_md(f'{max_total:.1f}%')} "
                    f"\\({_md(f'{total_dd/max_total*100:.0f}%')} of limit\\)"
                )
                self._ftmo_total_warned = True
            elif total_dd < total_warn_thresh * 0.5:
                self._ftmo_total_warned = False

            now_failed = bool(phase.get("failed"))
            now_passed = bool(phase.get("passed"))
            if now_failed and not self._ftmo_prev_failed:
                reason = phase.get("fail_reason", "")
                alerts.append(
                    f"\U0001f6ab *FTMO CHALLENGE FAILED*"
                    + (f"\nReason: {_md(reason)}" if reason else "")
                )
            if now_passed and not self._ftmo_prev_passed:
                _profit_str = f'+{phase.get("profit_pct", 0):.2f}%'
                alerts.append(
                    f"\U0001f3c6 *FTMO PHASE PASSED\\!*\n"
                    f"Profit: {_md(_profit_str)}"
                )
            self._ftmo_prev_failed = now_failed
            self._ftmo_prev_passed = now_passed

        # ── Per-symbol position changes ─────────────────────────────────────
        all_syms = set(cur.symbols) | set(prev.symbols)
        for sym in all_syms:
            c = cur.symbols.get(sym)
            p = prev.symbols.get(sym)
            c_pos = c.position if c else 0
            p_pos = p.position if p else 0

            if c_pos != 0 and p_pos == 0 and c:
                side = c.position_str
                lots = c.last_signal.get("lot", "?")
                price = c.entry_price or c.last_signal.get("price", 0)
                alerts.append(
                    f"\U0001f7e2 *Trade opened*: `{_md(sym)}` {_md(side)} "
                    f"{_md(str(lots))} lots @ {_md(f'{price:.5g}')}"
                )

            elif c_pos == 0 and p_pos != 0 and p:
                pnl = p.unrealized_pnl
                icon = "\U0001f7e2" if pnl >= 0 else "\U0001f534"
                alerts.append(
                    f"{icon} *Trade closed*: `{_md(sym)}` {_md(_fmt_pnl(pnl))}"
                )

        # ── Heartbeat lost ──────────────────────────────────────────────────
        hb_age = _age_seconds(cur.last_heartbeat)
        if hb_age > HB_WARN_SEC and not self._hb_warned:
            alerts.append(
                f"\u26a0\ufe0f Heartbeat lost for {_md(str(int(hb_age)))}s \u2014 "
                "Python publisher may be down"
            )
            self._hb_warned = True
        elif hb_age <= HB_WARN_SEC:
            self._hb_warned = False

        # ── Daily P&L summary at EOD ────────────────────────────────────────
        today_doy = now.timetuple().tm_yday
        if (
            now.hour == EOD_SUMMARY_HOUR_UTC
            and today_doy != self._last_summary_day
            and cur.equity > 0
        ):
            self._last_summary_day = today_doy
            daily_pnl = cur.equity - self._equity_at_session_open
            daily_pct = (daily_pnl / (self._equity_at_session_open + 1e-9)) * 100
            icon = "\U0001f4c8" if daily_pnl >= 0 else "\U0001f4c9"
            lines = [
                f"{icon} *Daily P&L Summary* \\({_md(now.strftime('%Y\\-%m\\-%d'))}\\)",
                f"P&L: {_md(_fmt_pnl(daily_pnl))} \\({_md(f'{daily_pct:+.2f}%')}\\)",
                f"Equity: {_md(f'${cur.equity:,.2f}')}",
                f"Drawdown: {_md(f'{cur.drawdown_pct:.2f}%')}",
                f"Signals: {_md(str(cur.signal_count))}",
            ]
            # Per-symbol summary
            for sym, s in sorted(cur.symbols.items()):
                regime = _md(s.regime_str)
                lines.append(f"  `{_md(sym)}` regime={regime} pos={_md(s.position_str)}")
            alerts.append("\n".join(lines))
            # Reset session-open equity for next day
            self._equity_at_session_open = cur.equity

        # ── Drift detection + model swap alerts from log ────────────────────
        alerts.extend(self._scan_log_alerts())

        self._prev = cur
        return alerts

    def _scan_log_alerts(self) -> list[str]:
        """Scan last lines of signal_server.log for drift/swap events."""
        alerts: list[str] = []
        try:
            import os as _os
            size = _os.path.getsize(_LOG_PATH)
        except Exception:
            return alerts

        if size <= self._last_log_size:
            return alerts                           # nothing new

        new_lines = _tail_log(_LOG_PATH, lines=50)
        self._last_log_size = size

        for line in new_lines:
            if "drift_detected" in line and "AutoRetrain START" in line:
                # Extract symbol if possible
                sym = ""
                if "[" in line:
                    try:
                        sym = line.split("[")[1].split("]")[0]
                    except Exception:
                        pass
                alerts.append(
                    f"\U0001f9e0 *Drift detected* on `{_md(sym)}`\n"
                    "AutoRetrainer triggered — retraining in background"
                )
            elif "New model ACCEPTED" in line:
                sym = ""
                if "[" in line:
                    try:
                        sym = line.split("[")[1].split("]")[0]
                    except Exception:
                        pass
                alerts.append(
                    f"\u2705 *Model swap* `{_md(sym)}` \u2014 new model ACCEPTED and deployed"
                )
            elif "New model REJECTED" in line:
                sym = ""
                if "[" in line:
                    try:
                        sym = line.split("[")[1].split("]")[0]
                    except Exception:
                        pass
                alerts.append(
                    f"\u274c *Model swap* `{_md(sym)}` \u2014 new model REJECTED, keeping current"
                )

        return alerts


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ATS Monitor bot running\\.\n"
        "Commands: /status /positions /stats",
        parse_mode="MarkdownV2",
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = read_state()
    if state.is_killed:
        badge = "\U0001f534 KILLED"
    elif state.is_alive:
        badge = "\U0001f7e2 LIVE"
    else:
        badge = "\u26aa OFFLINE"

    hb_age = _age_seconds(state.last_heartbeat)
    hb_str = _md(f"{int(hb_age)}s ago") if hb_age < float("inf") else "unknown"

    lines = [
        f"*System:* {badge}",
        f"*Equity:* {_md(f'${state.equity:,.2f}')}",
        f"*Balance:* {_md(f'${state.balance:,.2f}')}",
        f"*Drawdown:* {_md(f'{state.drawdown_pct:.2f}%')}",
        f"*Signals:* {state.signal_count}",
        f"*Heartbeat:* {hb_str}",
    ]

    # FTMO phase info (if available)
    ftmo = read_ftmo_state()
    if ftmo:
        phase = ftmo.get("phase", {})
        if phase:
            phase_name  = phase.get("phase", "?").upper()
            acct_size   = phase.get("account_size", 0)
            profit_pct  = phase.get("profit_pct", 0.0)
            profit_tgt  = phase.get("profit_target_pct", PROFIT_TARGET_PCT)
            daily_dd    = phase.get("daily_loss_pct", 0.0)
            max_daily   = phase.get("max_daily_loss_pct", DAILY_LOSS_LIMIT_PCT)
            total_dd    = phase.get("total_dd_pct", 0.0)
            max_total   = phase.get("max_total_loss_pct", MAX_DRAWDOWN_PCT)
            t_days      = phase.get("trading_days", 0)
            min_days    = phase.get("min_trading_days", 4)
            passed      = phase.get("passed", False)
            failed      = phase.get("failed", False)
            outcome = "\U0001f3c6 PASSED" if passed else ("\U0001f6ab FAILED" if failed else "\U0001f7e1 IN PROGRESS")
            lines += [
                "",
                f"*FTMO {_md(phase_name)} ${_md(f'{acct_size:,.0f}')}* — {outcome}",
                f"  Profit: {_md(f'{profit_pct:+.2f}%')} / target {_md(f'{profit_tgt:.0f}%')}",
                f"  Daily DD: {_md(f'{daily_dd:.2f}%')} / {_md(f'{max_daily:.1f}%')}",
                f"  Total DD: {_md(f'{total_dd:.2f}%')} / {_md(f'{max_total:.1f}%')}",
                f"  Trading days: {_md(str(t_days))} / {_md(str(min_days))} required",
            ]

    await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")


async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = read_state()
    if not state.symbols:
        await update.message.reply_text("No symbol data available.")
        return

    lines = ["*Open Positions*"]
    any_open = False
    for sym, s in sorted(state.symbols.items()):
        if s.position == 0:
            continue
        any_open = True
        lines.append(
            f"`{sym}` {s.position_str} @ {s.entry_price:.5g} "
            f"| PnL: {_fmt_pnl(s.unrealized_pnl)}"
        )

    if not any_open:
        lines.append("No open positions \\(all flat\\)")

    await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = read_state()
    total_pnl = sum(s.unrealized_pnl for s in state.symbols.values())
    open_syms = [sym for sym, s in state.symbols.items() if s.position != 0]
    flat_syms = [sym for sym, s in state.symbols.items() if s.position == 0]

    text = (
        f"*ATS Stats*\n"
        f"Signals processed: {state.signal_count}\n"
        f"Open positions: {_md(', '.join(open_syms) or 'none')}\n"
        f"Flat: {_md(', '.join(flat_syms) or 'none')}\n"
        f"Unrealized P&L: {_md(_fmt_pnl(total_pnl))}\n"
        f"Account drawdown: {_md(f'{state.drawdown_pct:.2f}%')}"
    )
    await update.message.reply_text(text, parse_mode="MarkdownV2")


# ---------------------------------------------------------------------------
# Background polling loop
# ---------------------------------------------------------------------------

async def _alert_loop(app: Application):
    """Poll live_state.json every POLL_INTERVAL seconds, send alert diffs."""
    differ = AlertDiffer()
    while True:
        await asyncio.sleep(POLL_INTERVAL)
        try:
            cur = read_state()
            alerts = differ.diff(cur)
            for msg in alerts:
                if CHAT_ID:
                    await app.bot.send_message(
                        chat_id=CHAT_ID,
                        text=msg,
                        parse_mode="MarkdownV2",
                    )
                    logger.info(f"Alert sent: {msg[:80]}")
        except Exception as exc:
            logger.warning(f"Alert loop error: {exc}")


async def _on_startup(app: Application):
    asyncio.create_task(_alert_loop(app))
    logger.info("Alert polling loop started")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    if not TOKEN:
        raise RuntimeError(
            "TELEGRAM_TOKEN env var not set. "
            "Get a token from @BotFather and set it before running."
        )

    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(_on_startup)
        .build()
    )

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("status",    cmd_status))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("stats",     cmd_stats))

    logger.info(
        f"Telegram bot starting — chat_id={'set' if CHAT_ID else 'NOT SET'}"
    )
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
