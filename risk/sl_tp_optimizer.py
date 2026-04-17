"""Adaptive SL/TP ATR-multiplier optimizer per (regime × session) bucket.

Method (no MAE/MFE data needed):
    For each (regime, session) bucket built from closed-trade history:

        win_rate  = Bayesian Beta(α+wins, β+losses) estimate
        optimal_R = (1 - win_rate) / win_rate   ← Kelly break-even reward/risk

    SL multiplier adjustment:
        win_rate > 0.60  → × 0.80  (high accuracy, can afford tighter stop)
        win_rate > 0.50  → × 1.00  (neutral — keep default)
        win_rate > 0.45  → × 1.15  (slightly widen to avoid shake-outs)
        win_rate > 0.40  → × 1.25
        else             → × 1.50  (low accuracy — must widen stop)

    TP multiplier:
        tp_mult = sl_mult × optimal_R × TP_SAFETY
        (ensures the trade has positive expectancy at minimum)

    Falls back to config atr_mult_sl when bucket has fewer than
    MIN_BUCKET_TRADES trades — first to regime-level aggregate, then
    to global config default.
"""
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config import (
    LOG_DIR, get_symbol_config,
    KELLY_PRIOR_ALPHA, KELLY_PRIOR_BETA,
    PNL_SCRATCH_THRESHOLD as _CFG_SCRATCH,
    TP_MULT_DEFAULT, TP_SAFETY_MARGIN, SL_ADJ_TABLE,
)

logger = logging.getLogger("sl_tp_optimizer")

# UTC session windows — (name, start_hour_inclusive, end_hour_exclusive)
_SESSIONS: list[tuple[str, int, int]] = [
    ("asia",    0,  7),
    ("london",  7, 13),
    ("overlap", 13, 17),
    ("ny",      17, 22),
]


def _session_for_hour(hour: int) -> str:
    for name, start, end in _SESSIONS:
        if start <= hour < end:
            return name
    return "ny"


class SLTPOptimizer:
    """Per-(regime, session) adaptive ATR multiplier optimizer.

    Usage:
        opt = SLTPOptimizer("XAUUSD")
        opt.record_trade(pnl=12.5, regime=1, timestamp=time.time())
        sl_dist = tick_atr * opt.get_sl_mult(regime=1, hour_utc=10)
        tp_dist = tick_atr * opt.get_tp_mult(regime=1, hour_utc=10)
    """

    MIN_BUCKET_TRADES = 10
    PRIOR_ALPHA       = KELLY_PRIOR_ALPHA
    PRIOR_BETA        = KELLY_PRIOR_BETA
    SCRATCH_THRESHOLD = _CFG_SCRATCH

    SL_MULT_MIN = 0.5
    SL_MULT_MAX = 4.0
    TP_MULT_MIN = 1.0
    TP_MULT_MAX = 8.0
    TP_SAFETY   = TP_SAFETY_MARGIN

    def __init__(self, symbol: str):
        self.symbol = symbol
        sym_cfg = get_symbol_config(symbol)
        self._default_sl: float = sym_cfg.get("atr_mult_sl", 1.5)
        self._default_tp: float = self._default_sl * TP_MULT_DEFAULT

        # (regime: int, session: str) → [{"pnl": float}, ...]
        self._trades: dict[tuple, list[dict]] = defaultdict(list)

        self._persist_path: Path = LOG_DIR / f"sl_tp_state_{symbol.lower()}.json"
        self._try_load()

    # ── persistence ──────────────────────────────────────────────────────────

    def _try_load(self) -> None:
        try:
            if self._persist_path.exists():
                raw = json.loads(self._persist_path.read_text())
                for key_str, trades in raw.items():
                    regime_str, session = key_str.split("|", 1)
                    self._trades[(int(regime_str), session)] = trades
                total = sum(len(v) for v in self._trades.values())
                logger.info("[%s] SLTPOptimizer: loaded %d trades across %d buckets",
                            self.symbol, total, len(self._trades))
        except Exception as exc:
            logger.warning("[%s] SLTPOptimizer load failed: %s", self.symbol, exc)

    def save(self) -> None:
        raw = {f"{k[0]}|{k[1]}": v for k, v in self._trades.items()}
        self._persist_path.write_text(json.dumps(raw))

    # ── data feed ────────────────────────────────────────────────────────────

    def record_trade(self, pnl: float, regime: int, timestamp: float) -> None:
        """Record a closed trade for bucket learning. Call after each deal close."""
        dt      = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        session = _session_for_hour(dt.hour)
        self._trades[(regime, session)].append({"pnl": pnl})
        self.save()

    # ── internal helpers ─────────────────────────────────────────────────────

    def _resolve_trades(self, regime: int, session: str) -> list[dict]:
        """Return trades for (regime, session), falling back to regime-only aggregate."""
        exact = self._trades.get((regime, session), [])
        if len(exact) >= self.MIN_BUCKET_TRADES:
            return exact
        # Aggregate all sessions for this regime
        agg = [t for (r, _), ts in self._trades.items() if r == regime for t in ts]
        return agg if len(agg) >= self.MIN_BUCKET_TRADES else []

    def _win_rate(self, trades: list[dict]) -> float:
        wins   = sum(1 for t in trades if t["pnl"] >  self.SCRATCH_THRESHOLD)
        losses = sum(1 for t in trades if t["pnl"] < -self.SCRATCH_THRESHOLD)
        a = self.PRIOR_ALPHA + wins
        b = self.PRIOR_BETA  + losses
        return a / (a + b)

    # ── public API ───────────────────────────────────────────────────────────

    def get_sl_mult(self, regime: int, hour_utc: int) -> float:
        """ATR multiplier for stop-loss. Wider when win rate is low."""
        trades = self._resolve_trades(regime, _session_for_hour(hour_utc))
        if not trades:
            return self._default_sl

        wr = self._win_rate(trades)
        adj = 1.50  # fallback (lowest entry in SL_ADJ_TABLE)
        for threshold, multiplier in SL_ADJ_TABLE:
            if wr > threshold:
                adj = multiplier
                break

        return float(np.clip(self._default_sl * adj, self.SL_MULT_MIN, self.SL_MULT_MAX))

    def get_tp_mult(self, regime: int, hour_utc: int) -> float:
        """ATR multiplier for take-profit.

        Derived from Kelly break-even R so the trade has positive expectancy:
            optimal_R = (1 - win_rate) / win_rate
            tp_mult   = sl_mult × optimal_R × TP_SAFETY
        """
        trades = self._resolve_trades(regime, _session_for_hour(hour_utc))
        sl_mult = self.get_sl_mult(regime, hour_utc)
        if not trades:
            return self._default_tp

        wr = self._win_rate(trades)
        if wr <= 0:
            return self._default_tp

        breakeven_r = (1.0 - wr) / wr
        tp_mult = sl_mult * breakeven_r * self.TP_SAFETY
        return float(np.clip(tp_mult, self.TP_MULT_MIN, self.TP_MULT_MAX))

    # ── diagnostics ──────────────────────────────────────────────────────────

    _SESSION_REPR_HOUR = {"asia": 3, "london": 9, "overlap": 15, "ny": 19}

    def diagnostics(self) -> dict:
        out: dict = {"symbol": self.symbol, "buckets": {}}
        for (regime, session), trades in self._trades.items():
            if not trades:
                continue
            wr = self._win_rate(trades)
            hour = self._SESSION_REPR_HOUR.get(session, 12)
            out["buckets"][f"regime={regime}|{session}"] = {
                "n_trades": len(trades),
                "win_rate": round(wr, 3),
                "sl_mult":  round(self.get_sl_mult(regime, hour), 3),
                "tp_mult":  round(self.get_tp_mult(regime, hour), 3),
            }
        return out
