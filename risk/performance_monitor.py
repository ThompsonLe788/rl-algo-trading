"""
PerformanceMonitor — rolling trade tracker for drift detection.

Records closed P&L for each trade and exposes win_rate / Sharpe metrics.
AutoRetrainer polls is_drifting() to decide when to trigger a retrain.
"""
from __future__ import annotations

import threading
from collections import deque

import numpy as np

from config import DRIFT_WIN_RATE_THRESHOLD, RETRAIN_MIN_TRADES


class PerformanceMonitor:
    """Thread-safe rolling window of closed trade P&L values.

    Args:
        symbol:  Instrument name (for logging).
        window:  Max trades to keep in rolling window (default 50).
    """

    def __init__(self, symbol: str, window: int = 50):
        self.symbol = symbol
        self._trades: deque[float] = deque(maxlen=window)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def record_trade(self, pnl: float) -> None:
        """Called when a position closes.  pnl is in price-point units."""
        with self._lock:
            self._trades.append(float(pnl))

    def reset(self) -> None:
        """Clear trade history (called after a successful model swap)."""
        with self._lock:
            self._trades.clear()

    # ------------------------------------------------------------------
    def win_rate(self) -> float:
        """Fraction of profitable trades in the rolling window."""
        with self._lock:
            t = list(self._trades)
        if not t:
            return 1.0
        return sum(1 for p in t if p > 0) / len(t)

    def sharpe(self) -> float:
        """Annualised Sharpe of raw P&L values in rolling window."""
        with self._lock:
            t = list(self._trades)
        if len(t) < 5:
            return 0.0
        arr = np.array(t, dtype=float)
        std = arr.std()
        return float(arr.mean() / std) if std > 0 else 0.0

    def is_drifting(self) -> bool:
        """True when enough trades exist AND win rate is below threshold."""
        with self._lock:
            n = len(self._trades)
        return n >= RETRAIN_MIN_TRADES and self.win_rate() < DRIFT_WIN_RATE_THRESHOLD

    def enough_data(self) -> bool:
        with self._lock:
            return len(self._trades) >= RETRAIN_MIN_TRADES

    def summary(self) -> dict:
        return {
            "symbol":       self.symbol,
            "n_trades":     len(self._trades),
            "win_rate":     round(self.win_rate(), 4),
            "sharpe":       round(self.sharpe(), 4),
            "is_drifting":  self.is_drifting(),
        }
