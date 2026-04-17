"""Kill switch: circuit breaker for catastrophic drawdown.

Hard stop if account drawdown exceeds MAX_DRAWDOWN_PCT.
Also enforces:
  - Daily loss limits
  - EOD liquidation
  - Trading-session filter  (London 08-17 GMT, New York 13-22 GMT)
  - News blackout windows   (15 min pre / 10 min post high-impact events)
"""
import logging
from datetime import datetime, timezone

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config import MAX_DRAWDOWN_PCT, DAILY_LOSS_LIMIT_PCT, EOD_HOUR_GMT, LOG_DIR

logger = logging.getLogger("kill_switch")
_handler = logging.FileHandler(LOG_DIR / "kill_switch.log")
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(_handler)
logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Per-symbol trading session windows  (minutes from midnight UTC)
# ---------------------------------------------------------------------------
# Format: list of (open_min, close_min) tuples — any overlap qualifies
_SESSION_MINUTES: dict[str, list[tuple[int, int]]] = {
    # XAUUSD: London open → NY close  (most liquid window for gold)
    "XAUUSD": [(8 * 60, 22 * 60)],

    # Majors: London + NY sessions; skip Asia dead-zone 00-07 UTC
    "EURUSD": [(7 * 60, 17 * 60), (13 * 60, 22 * 60)],
    "GBPUSD": [(7 * 60, 17 * 60), (13 * 60, 22 * 60)],
    "AUDUSD": [(0 * 60,  7 * 60), (7 * 60, 17 * 60), (13 * 60, 22 * 60)],

    # JPY pairs: include Asian session (Tokyo)
    "USDJPY": [(0 * 60,  3 * 60), (7 * 60, 17 * 60), (13 * 60, 22 * 60)],
    "EURJPY": [(0 * 60,  3 * 60), (7 * 60, 17 * 60), (13 * 60, 22 * 60)],

    # Crypto: 24h but skip extreme low-liquidity hours  (03-06 UTC)
    "BTCUSD": [(0 * 60,  3 * 60), (6 * 60, 24 * 60)],

    # Indices: trade only during their primary session
    "NAS100": [(13 * 60, 21 * 60)],    # NYSE hours
    "US30":   [(13 * 60, 21 * 60)],
}
# Default for unknown symbols: London + NY overlap
_DEFAULT_SESSIONS: list[tuple[int, int]] = [(7 * 60, 22 * 60)]


def _in_session(symbol: str, now: datetime) -> bool:
    """True if current UTC time falls within any trading session for symbol."""
    minutes = now.hour * 60 + now.minute
    windows = _SESSION_MINUTES.get(symbol.upper(), _DEFAULT_SESSIONS)
    return any(start <= minutes < end for start, end in windows)


class KillSwitch:
    """Account-level risk circuit breaker.

    Args:
        max_drawdown_pct:    Hard-kill drawdown threshold (default 15 %).
        daily_loss_limit_pct: Intraday loss limit (default 5 %).
        eod_hour_gmt:        Hour (UTC) at which EOD liquidation fires.
        news_filter:         Optional NewsFilter instance. Pass ``None`` to
                             disable news blocking (e.g. in backtests).
        symbol:              Instrument name — used for session-window lookup.
        session_filter:      Enable/disable trading-session checks.
    """

    def __init__(
        self,
        max_drawdown_pct: float = MAX_DRAWDOWN_PCT,
        daily_loss_limit_pct: float = DAILY_LOSS_LIMIT_PCT,
        eod_hour_gmt: int = EOD_HOUR_GMT,
        news_filter=None,          # NewsFilter | None
        symbol: str = "",
        session_filter: bool = True,
    ):
        if session_filter and not symbol:
            raise ValueError("KillSwitch: symbol is required when session_filter=True")
        self.max_dd_pct = max_drawdown_pct
        self.daily_loss_pct = daily_loss_limit_pct
        self.eod_hour = eod_hour_gmt
        self._news_filter = news_filter
        self._symbol = symbol.upper()
        self._session_filter = session_filter

        self.peak_equity = 0.0
        self.session_start_equity = 0.0
        self.is_killed = False
        self.kill_reason = ""
        self._eod_logged_hour: int = -1       # throttle EOD log
        self._session_logged_min: int = -1    # throttle session log
        self._last_reset_date = None          # date of last daily reset

    # ------------------------------------------------------------------
    # Equity tracking
    # ------------------------------------------------------------------

    def update_equity(self, current_equity: float):
        if current_equity > self.peak_equity:
            self.peak_equity = current_equity

    def set_session_start(self, equity: float):
        self.session_start_equity = equity
        self.peak_equity = max(self.peak_equity, equity)

    def drawdown_pct(self, current_equity: float) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return (self.peak_equity - current_equity) / self.peak_equity * 100.0

    def daily_loss_pct_current(self, current_equity: float) -> float:
        if self.session_start_equity <= 0:
            return 0.0
        return (self.session_start_equity - current_equity) / self.session_start_equity * 100.0

    # ------------------------------------------------------------------
    # Time-based checks
    # ------------------------------------------------------------------

    def is_eod(self) -> bool:
        return datetime.now(timezone.utc).hour >= self.eod_hour

    def no_new_trades_allowed(self) -> bool:
        return datetime.now(timezone.utc).hour >= self.eod_hour - 1

    def _in_session_now(self) -> bool:
        """True if NOW is inside a valid trading session for this symbol."""
        if not self._session_filter:
            return True
        return _in_session(self._symbol, datetime.now(timezone.utc))

    def _news_blackout_now(self) -> tuple[bool, str]:
        """True + event name if inside a news blackout window."""
        if self._news_filter is None:
            return False, ""
        try:
            return self._news_filter.is_blackout(currencies=["USD"])
        except Exception:
            return False, ""

    # ------------------------------------------------------------------
    # Main check
    # ------------------------------------------------------------------

    def check(self, current_equity: float) -> dict:
        """Run all kill-switch checks. Returns status dict.

        Returns:
            {
                "should_close_all":  bool,
                "allow_new_trades":  bool,
                "reason":            str,
                "drawdown_pct":      float,
                "daily_loss_pct":    float,
            }
        """
        # ── 0. Auto daily reset at UTC midnight ────────────────────────────────
        today = datetime.now(timezone.utc).date()
        if self._last_reset_date is not None and today != self._last_reset_date:
            logger.info(f"Auto daily reset (new UTC day: {today}). Previous session equity: {self.session_start_equity:.2f}")
            self.session_start_equity = current_equity
            self.is_killed = False
            self.kill_reason = ""
        self._last_reset_date = today

        # Guard: invalid equity
        if current_equity <= 0:
            return {
                "should_close_all": False,
                "allow_new_trades": False,
                "reason": "equity_unavailable",
                "drawdown_pct": 0.0,
                "daily_loss_pct": 0.0,
            }

        self.update_equity(current_equity)
        dd = self.drawdown_pct(current_equity)
        daily_loss = self.daily_loss_pct_current(current_equity)

        result = {
            "should_close_all": False,
            "allow_new_trades": True,
            "reason": "",
            "drawdown_pct": dd,
            "daily_loss_pct": daily_loss,
        }

        # ── 1. Max drawdown kill ────────────────────────────────────────────
        if dd >= self.max_dd_pct:
            result["should_close_all"] = True
            result["allow_new_trades"] = False
            result["reason"] = f"MAX DRAWDOWN {dd:.1f}% >= {self.max_dd_pct}%"
            self.is_killed = True
            self.kill_reason = result["reason"]
            logger.critical(result["reason"])
            return result

        # ── 2. Daily loss limit ─────────────────────────────────────────────
        if daily_loss >= self.daily_loss_pct:
            result["should_close_all"] = True
            result["allow_new_trades"] = False
            result["reason"] = f"DAILY LOSS {daily_loss:.1f}% >= {self.daily_loss_pct}%"
            logger.warning(result["reason"])
            return result

        # ── 3. EOD liquidation ──────────────────────────────────────────────
        if self.is_eod():
            result["should_close_all"] = True
            result["allow_new_trades"] = False
            result["reason"] = f"EOD liquidation (>= {self.eod_hour}:00 GMT)"
            now_h = datetime.now(timezone.utc).hour
            if now_h != self._eod_logged_hour:
                logger.info(result["reason"])
                self._eod_logged_hour = now_h
            return result

        # ── 4. Pre-EOD no-new-trades window ────────────────────────────────
        if self.no_new_trades_allowed():
            result["allow_new_trades"] = False
            result["reason"] = f"No new trades after {self.eod_hour - 1}:00 GMT"
            return result

        # ── 5. Trading session filter ───────────────────────────────────────
        if not self._in_session_now():
            result["allow_new_trades"] = False
            now = datetime.now(timezone.utc)
            cur_min = now.hour * 60 + now.minute
            result["reason"] = f"Outside trading session ({now.strftime('%H:%M')} UTC)"
            if cur_min != self._session_logged_min:
                logger.info(f"[{self._symbol}] " + result["reason"])
                self._session_logged_min = cur_min
            return result

        # ── 6. News blackout ────────────────────────────────────────────────
        news_blocked, event_name = self._news_blackout_now()
        if news_blocked:
            result["allow_new_trades"] = False
            result["reason"] = f"News blackout: {event_name}"
            logger.info(f"[{self._symbol}] {result['reason']}")
            return result

        return result

    def reset_daily(self, equity: float):
        """Reset daily counters for new session."""
        self.session_start_equity = equity
        self.is_killed = False
        self.kill_reason = ""
        logger.info(f"Daily reset. Session start equity: {equity:.2f}")
