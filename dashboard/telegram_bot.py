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
"""
import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dashboard.state_reader import read_state, LiveState, SymbolState

from telegram import Update
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes

logger = logging.getLogger("telegram_bot")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
POLL_INTERVAL = 10  # seconds between state diffs
HB_WARN_SEC   = 45  # warn if heartbeat older than this


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

class AlertDiffer:
    """Track previous state to detect changes worth alerting."""

    def __init__(self):
        self._prev: LiveState | None = None
        self._hb_warned = False   # suppress repeated HB warnings

    def diff(self, cur: LiveState) -> list[str]:
        """Return list of alert message strings (may be empty)."""
        if self._prev is None:
            self._prev = cur
            return []

        alerts: list[str] = []
        prev = self._prev

        # Kill switch transition
        if cur.is_killed and not prev.is_killed:
            reason = cur.system.get("kill_reason", "")
            alerts.append(
                f"\U0001f534 *KILL SWITCH* activated\\!\n"
                f"Drawdown: {cur.drawdown_pct:.1f}%"
                + (f"\nReason: {reason}" if reason else "")
            )

        # Drawdown warning (crosses 5%)
        if cur.drawdown_pct >= 5.0 and prev.drawdown_pct < 5.0:
            alerts.append(
                f"\u26a0\ufe0f Daily drawdown {cur.drawdown_pct:.1f}% — approaching kill limit"
            )

        # Per-symbol position changes
        all_syms = set(cur.symbols) | set(prev.symbols)
        for sym in all_syms:
            c = cur.symbols.get(sym)
            p = prev.symbols.get(sym)
            c_pos = c.position if c else 0
            p_pos = p.position if p else 0

            # Position opened
            if c_pos != 0 and p_pos == 0 and c:
                side = c.position_str
                lots = c.last_signal.get("lot", "?")
                price = c.entry_price or c.last_signal.get("price", 0)
                alerts.append(
                    f"\U0001f7e2 *Trade opened*: `{sym}` {side} {lots} lots @ {price:.5g}"
                )

            # Position closed
            elif c_pos == 0 and p_pos != 0 and p:
                pnl = p.unrealized_pnl
                alerts.append(
                    f"\u26aa *Trade closed*: `{sym}` {_fmt_pnl(pnl)}"
                )

        # Heartbeat lost
        hb_age = _age_seconds(cur.last_heartbeat)
        if hb_age > HB_WARN_SEC and not self._hb_warned:
            alerts.append(
                f"\u26a0\ufe0f Heartbeat lost for {int(hb_age)}s — "
                f"Python publisher may be down"
            )
            self._hb_warned = True
        elif hb_age <= HB_WARN_SEC:
            self._hb_warned = False  # reset

        self._prev = cur
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

    text = (
        f"*System:* {badge}\n"
        f"*Equity:* {_md(f'${state.equity:,.2f}')}\n"
        f"*Balance:* {_md(f'${state.balance:,.2f}')}\n"
        f"*Drawdown:* {_md(f'{state.drawdown_pct:.2f}%')}\n"
        f"*Signals:* {state.signal_count}\n"
        f"*Heartbeat:* {hb_str}"
    )
    await update.message.reply_text(text, parse_mode="MarkdownV2")


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
