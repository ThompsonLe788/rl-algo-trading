"""Kill switch: circuit breaker for catastrophic drawdown.

Hard stop if account drawdown exceeds MAX_DRAWDOWN_PCT.
Also enforces EOD liquidation and daily loss limits.
"""
import time
import logging
from datetime import datetime, timezone

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config import MAX_DRAWDOWN_PCT, EOD_HOUR_GMT, LOG_DIR

logger = logging.getLogger("kill_switch")
_handler = logging.FileHandler(LOG_DIR / "kill_switch.log")
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(_handler)
logger.setLevel(logging.INFO)


class KillSwitch:
    """Account-level risk circuit breaker."""

    def __init__(
        self,
        max_drawdown_pct: float = MAX_DRAWDOWN_PCT,
        daily_loss_limit_pct: float = 5.0,
        eod_hour_gmt: int = EOD_HOUR_GMT,
    ):
        self.max_dd_pct = max_drawdown_pct
        self.daily_loss_pct = daily_loss_limit_pct
        self.eod_hour = eod_hour_gmt

        self.peak_equity = 0.0
        self.session_start_equity = 0.0
        self.is_killed = False
        self.kill_reason = ""

    def update_equity(self, current_equity: float):
        """Track peak equity for drawdown calculation."""
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

    def is_eod(self) -> bool:
        now = datetime.now(timezone.utc)
        return now.hour >= self.eod_hour

    def no_new_trades_allowed(self) -> bool:
        now = datetime.now(timezone.utc)
        return now.hour >= self.eod_hour - 1

    def check(self, current_equity: float) -> dict:
        """Run all kill-switch checks. Returns status dict.

        Returns:
            {
                "should_close_all": bool,
                "allow_new_trades": bool,
                "reason": str,
                "drawdown_pct": float,
                "daily_loss_pct": float,
            }
        """
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

        # MDD kill
        if dd >= self.max_dd_pct:
            result["should_close_all"] = True
            result["allow_new_trades"] = False
            result["reason"] = f"MAX DRAWDOWN {dd:.1f}% >= {self.max_dd_pct}%"
            self.is_killed = True
            self.kill_reason = result["reason"]
            logger.critical(result["reason"])

        # Daily loss limit
        elif daily_loss >= self.daily_loss_pct:
            result["should_close_all"] = True
            result["allow_new_trades"] = False
            result["reason"] = f"DAILY LOSS {daily_loss:.1f}% >= {self.daily_loss_pct}%"
            logger.warning(result["reason"])

        # EOD liquidation
        elif self.is_eod():
            result["should_close_all"] = True
            result["allow_new_trades"] = False
            result["reason"] = f"EOD liquidation (>= {self.eod_hour}:00 GMT)"
            logger.info(result["reason"])

        # No new trades window
        elif self.no_new_trades_allowed():
            result["allow_new_trades"] = False
            result["reason"] = f"No new trades after {self.eod_hour - 1}:00 GMT"

        return result

    def reset_daily(self, equity: float):
        """Reset daily counters for new session."""
        self.session_start_equity = equity
        self.is_killed = False
        self.kill_reason = ""
        logger.info(f"Daily reset. Session start equity: {equity:.2f}")
