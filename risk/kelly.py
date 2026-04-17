"""Enhanced Fractional Kelly position sizing — symbol-agnostic.

f* = (1/fraction_denom) * (p*b - q) / b

Features:
- Per-symbol leverage and contract size from SYMBOL_CONFIGS
- Rolling 200-trade Bayesian win rate (Beta(α,β) posterior)
- Hard cap at 2% risk per trade
- Drawdown-scaled: cuts size linearly as DD grows
- Regime-aware: halves size in uncertain/range regimes
- Edge-decay detection: zeros out if recent edge is negative
- Streak dampener: reduces size on losing streaks
- Margin-aware: respects broker margin requirements
- Per-symbol JSON persistence for restart recovery
- Portfolio Kelly: correlation-adjusted equity allocation across symbols
"""
import json
import logging
import math
import threading
from collections import deque
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Literal

import numpy as np

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config import (
    KELLY_FRACTION, MAX_RISK_PER_TRADE, LEVERAGE, ROLLING_WINDOW,
    LOG_DIR, MAX_DRAWDOWN_PCT, get_symbol_config,
)

logger = logging.getLogger("kelly")


# ---------------------------------------------------------------------------
# Trade record
# ---------------------------------------------------------------------------
@dataclass
class TradeRecord:
    pnl: float
    timestamp: float = 0.0   # epoch seconds
    regime: int = -1          # 0=range, 1=trend, -1=unknown


# ---------------------------------------------------------------------------
# Enhanced Fractional Kelly Position Sizer
# ---------------------------------------------------------------------------
class KellyPositionSizer:
    """Fractional Kelly with rolling 200-trade Bayesian stats,
    drawdown scaling, regime adjustment, and edge-decay guard."""

    # -- Bayesian prior: Beta(2,2) → mildly informative 50% prior
    PRIOR_ALPHA = 2.0
    PRIOR_BETA = 2.0

    # -- Minimum trades before trusting Kelly (use fixed fractional before)
    MIN_TRADES_FOR_KELLY = 30

    # -- Drawdown scaling: at dd_full_pct, size → 0
    DD_TAPER_START = 5.0   # start tapering at 5% DD
    DD_TAPER_END = MAX_DRAWDOWN_PCT  # fully off at kill-switch level

    # -- Edge-decay: if last N trades have negative expectancy → skip
    EDGE_DECAY_WINDOW = 20

    # -- Losing streak dampener: reduce by 25% per consecutive loss
    STREAK_DECAY = 0.25
    MAX_STREAK_PENALTY = 0.75  # never cut more than 75%

    # -- Volatility regime: number of recent bars tracked for rvol z-score
    VOL_HISTORY_LEN = 500   # ~8 hours of M1 data

    def __init__(
        self,
        symbol: str = "XAUUSD",
        fraction: float = KELLY_FRACTION,
        max_risk: float = MAX_RISK_PER_TRADE,
        leverage: int | None = None,
        rolling_window: int = ROLLING_WINDOW,
        persist_path: Path | None = None,
    ):
        self.symbol   = symbol.upper()
        self.fraction = fraction
        self.max_risk = max_risk
        self.rolling_window = rolling_window
        self.trade_history: deque[TradeRecord] = deque(maxlen=rolling_window)

        # Per-symbol leverage and margin from SYMBOL_CONFIGS
        sym_cfg       = get_symbol_config(self.symbol)
        self.leverage = leverage if leverage is not None else sym_cfg["leverage"]
        self.MARGIN_PCT = 1.0 / self.leverage   # standard broker margin formula

        self.persist_path = persist_path or (
            LOG_DIR / f"kelly_state_{self.symbol.lower()}.json"
        )

        # Drawdown tracking (fed externally)
        self._current_dd_pct: float = 0.0

        # Active regime (fed externally)
        self._regime: int = -1  # 0=range, 1=trend

        # Volatility regime: rolling realized-vol history for z-score (⑩)
        self._rvol_history: deque[float] = deque(maxlen=self.VOL_HISTORY_LEN)

        self._try_load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _try_load(self):
        try:
            if self.persist_path.exists():
                data = json.loads(self.persist_path.read_text())
                for rec in data.get("trades", []):
                    self.trade_history.append(TradeRecord(**rec))
                logger.info("Loaded %d trades from %s", len(self.trade_history), self.persist_path)
        except Exception as e:
            logger.warning("Could not load kelly state: %s", e)

    def save(self):
        data = {"trades": [asdict(t) for t in self.trade_history]}
        self.persist_path.write_text(json.dumps(data))

    # ------------------------------------------------------------------
    # External state feeds
    # ------------------------------------------------------------------
    def set_drawdown(self, dd_pct: float):
        self._current_dd_pct = max(dd_pct, 0.0)

    def set_regime(self, regime: int):
        self._regime = regime

    def update_rvol(self, rvol: float):
        """Feed current realized volatility for macro vol-regime tracking. (⑩)"""
        if rvol > 0:
            self._rvol_history.append(rvol)

    # ------------------------------------------------------------------
    # Record trades
    # ------------------------------------------------------------------
    def record_trade(self, pnl: float, timestamp: float = 0.0, regime: int = -1):
        self.trade_history.append(TradeRecord(pnl=pnl, timestamp=timestamp, regime=regime))
        self.save()

    # ------------------------------------------------------------------
    # Rolling Bayesian win rate  Beta(α + wins, β + losses)
    # ------------------------------------------------------------------
    # Minimum PnL magnitude to count as a win or loss (1 pip = $0.01 equivalent).
    # Trades with |pnl| < this threshold are treated as scratch/neutral and excluded
    # from the Bayesian win-rate to avoid inflating the loss count with break-evens.
    PNL_SCRATCH_THRESHOLD = 0.01

    @property
    def win_rate(self) -> float:
        wins   = sum(1 for t in self.trade_history if t.pnl >  self.PNL_SCRATCH_THRESHOLD)
        losses = sum(1 for t in self.trade_history if t.pnl < -self.PNL_SCRATCH_THRESHOLD)
        alpha = self.PRIOR_ALPHA + wins
        beta  = self.PRIOR_BETA  + losses
        return alpha / (alpha + beta)

    @property
    def win_rate_ci_lower(self) -> float:
        """5th percentile of Beta posterior — conservative bound."""
        wins   = sum(1 for t in self.trade_history if t.pnl >  self.PNL_SCRATCH_THRESHOLD)
        losses = sum(1 for t in self.trade_history if t.pnl < -self.PNL_SCRATCH_THRESHOLD)
        from scipy.stats import beta as beta_dist
        a = self.PRIOR_ALPHA + wins
        b = self.PRIOR_BETA + losses
        return float(beta_dist.ppf(0.05, a, b))

    @property
    def avg_win(self) -> float:
        wins = [t.pnl for t in self.trade_history if t.pnl > self.PNL_SCRATCH_THRESHOLD]
        return float(np.mean(wins)) if wins else 1.0

    @property
    def avg_loss(self) -> float:
        losses = [abs(t.pnl) for t in self.trade_history if t.pnl < -self.PNL_SCRATCH_THRESHOLD]
        return float(np.mean(losses)) if losses else 1.0

    @property
    def reward_risk_ratio(self) -> float:
        return self.avg_win / (self.avg_loss + 1e-9)

    @property
    def expectancy(self) -> float:
        """Per-trade expected PnL: p*W - q*L."""
        p = self.win_rate
        return p * self.avg_win - (1 - p) * self.avg_loss

    @property
    def num_trades(self) -> int:
        return len(self.trade_history)

    # ------------------------------------------------------------------
    # Losing streak
    # ------------------------------------------------------------------
    @property
    def consecutive_losses(self) -> int:
        streak = 0
        for t in reversed(self.trade_history):
            if t.pnl < -self.PNL_SCRATCH_THRESHOLD:
                streak += 1
            else:
                break
        return streak

    # ------------------------------------------------------------------
    # Edge decay: recent sub-window expectancy
    # ------------------------------------------------------------------
    @property
    def recent_edge_alive(self) -> bool:
        if len(self.trade_history) < self.EDGE_DECAY_WINDOW:
            return True
        recent = list(self.trade_history)[-self.EDGE_DECAY_WINDOW:]
        wins = sum(1 for t in recent if t.pnl > self.PNL_SCRATCH_THRESHOLD)
        p = wins / len(recent)
        avg_w = np.mean([t.pnl for t in recent if t.pnl > self.PNL_SCRATCH_THRESHOLD]) if wins else 0
        losses_r = [abs(t.pnl) for t in recent if t.pnl < -self.PNL_SCRATCH_THRESHOLD]
        avg_l = np.mean(losses_r) if losses_r else 0
        edge = p * avg_w - (1 - p) * avg_l
        return edge > 0

    # ------------------------------------------------------------------
    # Kelly core
    # ------------------------------------------------------------------
    def kelly_fraction_raw(self, p: float | None = None, b: float | None = None) -> float:
        """Raw Kelly: (p*b - q) / b."""
        if p is None:
            p = self.win_rate
        if b is None:
            b = self.reward_risk_ratio
        q = 1.0 - p
        if b <= 0:
            return 0.0
        f = (p * b - q) / b
        return max(f, 0.0)

    def _drawdown_scalar(self) -> float:
        """Linear taper: 1.0 at dd<=start, 0.0 at dd>=end.
        Clamped to [0, 1] so extreme DD beyond taper_end never goes negative."""
        if self._current_dd_pct <= self.DD_TAPER_START:
            return 1.0
        if self._current_dd_pct >= self.DD_TAPER_END:
            return 0.0
        raw = 1.0 - (self._current_dd_pct - self.DD_TAPER_START) / (self.DD_TAPER_END - self.DD_TAPER_START)
        return max(0.0, min(1.0, raw))

    def _regime_scalar(self) -> float:
        """Halve size in range regime (0), full in trend (1), 75% if unknown."""
        if self._regime == 0:
            return 0.5
        if self._regime == 1:
            return 1.0
        return 0.75  # unknown

    def _streak_scalar(self) -> float:
        """Reduce size on consecutive losses."""
        losses = self.consecutive_losses
        if losses == 0:
            return 1.0
        penalty = min(losses * self.STREAK_DECAY, self.MAX_STREAK_PENALTY)
        return 1.0 - penalty

    def _vol_regime_scalar(self) -> float:
        """Scale down sizing when realized volatility is elevated. (⑩)

        Compares current rvol (last reading) against the rolling mean/std
        of the past VOL_HISTORY_LEN bars:
          rvol z-score > 2σ  → 0.50× (half size — macro vol spike)
          rvol z-score > 1σ  → 0.75× (cautious — elevated vol)
          otherwise          → 1.00×
        """
        if len(self._rvol_history) < 30:
            return 1.0
        hist = list(self._rvol_history)
        current = hist[-1]
        mean_v = float(np.mean(hist))
        std_v  = float(np.std(hist)) + 1e-9
        z = (current - mean_v) / std_v
        if z > 2.0:
            return 0.50
        if z > 1.0:
            return 0.75
        return 1.0

    def optimal_fraction(self, p: float | None = None, b: float | None = None) -> float:
        """Enhanced fractional Kelly with all guards applied.

        Guards (multiplicative):
        1. Fractional Kelly (1/10)
        2. Drawdown taper
        3. Regime scalar
        4. Losing-streak dampener
        5. Edge-decay kill
        6. Hard cap at max_risk (2%)
        """
        # Not enough trades → conservative fixed fractional
        if self.num_trades < self.MIN_TRADES_FOR_KELLY:
            base = self.max_risk * 0.5  # half the cap while learning
        else:
            raw = self.kelly_fraction_raw(p, b)
            base = self.fraction * raw

        # Edge-decay gate
        if not self.recent_edge_alive:
            logger.warning("Edge decay detected — sizing zeroed")
            return 0.0

        # Multiplicative scalars (⑩ vol_regime_scalar added)
        f = (
            base
            * self._drawdown_scalar()
            * self._regime_scalar()
            * self._streak_scalar()
            * self._vol_regime_scalar()
        )

        return min(max(f, 0.0), self.max_risk)

    # ------------------------------------------------------------------
    # Lot size calculator
    # ------------------------------------------------------------------
    def calc_lot_size(
        self,
        account_equity: float,
        entry_price: float,
        sl_distance: float,
        contract_size: float | None = None,   # defaults to per-symbol config
        p: float | None = None,
        b: float | None = None,
    ) -> float:
        """Calculate lot size with all Kelly guards + margin check.

        lot = (equity * f*) / (sl_distance * contract_size)
        Capped by leverage and free-margin constraints.
        contract_size defaults to SYMBOL_CONFIGS[symbol]["contract_size"].
        """
        if contract_size is None:
            contract_size = get_symbol_config(self.symbol)["contract_size"]
        f = self.optimal_fraction(p, b)
        if f <= 0:
            return 0.0

        risk_amount = account_equity * f
        if sl_distance <= 0:
            logger.warning("[%s] sl_distance <= 0 (got %.6f) — lot=0. ATR missing?", self.symbol, sl_distance)
            return 0.0

        lot = risk_amount / (sl_distance * contract_size)

        # Leverage cap: notional / equity <= leverage
        max_notional = account_equity * self.leverage
        max_lot_leverage = max_notional / (entry_price * contract_size)
        lot = min(lot, max_lot_leverage)

        # Margin cap: ensure required margin doesn't exceed equity
        margin_per_lot = entry_price * contract_size * self.MARGIN_PCT
        if margin_per_lot > 0:
            max_lot_margin = (account_equity * 0.9) / margin_per_lot  # keep 10% buffer
            lot = min(lot, max_lot_margin)

        # Symbol max_lot hard cap (from SYMBOL_CONFIGS)
        max_lot_sym = get_symbol_config(self.symbol).get("max_lot", float("inf"))
        lot = min(lot, max_lot_sym)

        # Floor + round to per-symbol lot step (configurable via lot_precision).
        lp = get_symbol_config(self.symbol).get("lot_precision", 2)
        lot = max(round(lot, lp), 10 ** (-lp))
        return lot

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------
    def diagnostics(self) -> dict:
        return {
            "num_trades": self.num_trades,
            "win_rate_bayesian": round(self.win_rate, 4),
            "reward_risk_ratio": round(self.reward_risk_ratio, 4),
            "expectancy": round(self.expectancy, 4),
            "kelly_raw": round(self.kelly_fraction_raw(), 4),
            "optimal_f": round(self.optimal_fraction(), 4),
            "drawdown_pct": round(self._current_dd_pct, 2),
            "drawdown_scalar": round(self._drawdown_scalar(), 4),
            "regime": self._regime,
            "regime_scalar": round(self._regime_scalar(), 4),
            "consecutive_losses": self.consecutive_losses,
            "streak_scalar": round(self._streak_scalar(), 4),
            "vol_regime_scalar": round(self._vol_regime_scalar(), 4),
            "edge_alive": self.recent_edge_alive,
        }


# ---------------------------------------------------------------------------
# Portfolio Kelly — correlation-adjusted multi-symbol equity allocation
# ---------------------------------------------------------------------------

class PortfolioKellyAllocator:
    """Singleton that allocates account equity across active symbols using the
    inverse correlation matrix (Portfolio Kelly).

    When symbols trade independently (ρ≈0), each gets equity/N — identical to
    the naive split.  When two symbols are correlated (e.g. XAUUSD & GBPUSD
    both move on USD news), the inverse-correlation weights automatically reduce
    the budget of the correlated pair, capping total portfolio risk.

    Math:
        R  = (N × T) return matrix, one row per symbol
        C  = corrcoef(R) + ε·I   (regularised correlation matrix)
        w  = C⁻¹ · 1  /  (1ᵀ · C⁻¹ · 1)   (max-diversification weights)
        budget_i = equity × w_i

    Negative weights are floored to 0 and renormalised, so a strongly anti-
    correlated symbol still gets a small positive allocation.

    Usage (thread-safe singleton):
        alloc = PortfolioKellyAllocator.instance()
        alloc.update_return("XAUUSD", ret)          # call each tick
        budget = alloc.equity_budget("XAUUSD", equity, active_symbols)
    """

    RETURN_WINDOW     = 60    # rolling bars of returns per symbol
    MIN_OBS           = 20    # fall back to equity/N if fewer observations
    REGULARISE        = 0.05  # diagonal regularisation (ε) to avoid singularity
    HIGH_CORR_WARN    = 0.70  # log warning when any pair exceeds this

    _instance: "PortfolioKellyAllocator | None" = None
    _cls_lock = threading.Lock()

    @classmethod
    def instance(cls) -> "PortfolioKellyAllocator":
        if cls._instance is None:
            with cls._cls_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        self._returns: dict[str, deque] = {}
        self._lock = threading.Lock()

    # ── data feed ────────────────────────────────────────────────────────────

    def update_return(self, symbol: str, ret: float) -> None:
        """Feed one tick/bar return (price change / previous price)."""
        with self._lock:
            if symbol not in self._returns:
                self._returns[symbol] = deque(maxlen=self.RETURN_WINDOW)
            if np.isfinite(ret):
                self._returns[symbol].append(ret)

    # ── allocation ───────────────────────────────────────────────────────────

    def equity_budget(
        self,
        symbol: str,
        total_equity: float,
        active_symbols: list[str],
    ) -> float:
        """Return the equity budget for *symbol* after correlation adjustment.

        Falls back to total_equity / N when:
        - only one symbol is active
        - fewer than MIN_OBS returns are available for any symbol
        - the correlation matrix is numerically singular
        """
        n = len(active_symbols)
        if n <= 1:
            return total_equity

        fallback = total_equity / n

        with self._lock:
            if not all(
                sym in self._returns and len(self._returns[sym]) >= self.MIN_OBS
                for sym in active_symbols
            ):
                return fallback

            min_len = min(len(self._returns[s]) for s in active_symbols)
            R = np.array([list(self._returns[s])[-min_len:] for s in active_symbols], dtype=float)

            try:
                C = np.corrcoef(R)
                C = C + np.eye(n) * self.REGULARISE   # regularise

                # Warn on high pairwise correlations
                for i in range(n):
                    for j in range(i + 1, n):
                        if abs(C[i, j]) >= self.HIGH_CORR_WARN:
                            logger.warning(
                                "High correlation %.2f between %s and %s — "
                                "portfolio Kelly reducing allocation",
                                C[i, j], active_symbols[i], active_symbols[j],
                            )

                C_inv = np.linalg.inv(C)
                ones  = np.ones(n)
                raw_w = C_inv @ ones             # unnormalised weights
                total_w = ones @ raw_w
                if total_w <= 0:
                    return fallback

                weights = raw_w / total_w        # normalise → sum to 1
                weights = np.maximum(weights, 0.0)   # floor negatives
                s = weights.sum()
                if s <= 0:
                    return fallback
                weights /= s                     # renormalise after floor

                idx = active_symbols.index(symbol)
                return float(total_equity * weights[idx])

            except (np.linalg.LinAlgError, ValueError, IndexError):
                return fallback

    # ── diagnostics ──────────────────────────────────────────────────────────

    def correlation_matrix(self, symbols: list[str]) -> np.ndarray | None:
        """Return the current NxN correlation matrix, or None if insufficient data."""
        with self._lock:
            if not all(
                sym in self._returns and len(self._returns[sym]) >= self.MIN_OBS
                for sym in symbols
            ):
                return None
            min_len = min(len(self._returns[s]) for s in symbols)
            R = np.array([list(self._returns[s])[-min_len:] for s in symbols], dtype=float)
            return np.corrcoef(R)

    def weights(self, active_symbols: list[str]) -> dict[str, float]:
        """Return current allocation weights as a dict (sums to 1.0)."""
        n = len(active_symbols)
        if n == 0:
            return {}
        dummy_eq = 1.0
        return {
            sym: self.equity_budget(sym, dummy_eq, active_symbols)
            for sym in active_symbols
        }


# ---------------------------------------------------------------------------
# VWAP order slicer
# ---------------------------------------------------------------------------
def vwap_slice_orders(
    total_lot: float,
    base_price: float,
    atr: float,
    num_slices: int = 5,
    atr_spread: float = 0.3,
    min_lot: float = 0.01,
) -> list[dict]:
    """Split large orders into child limit orders around VWAP ± spread*ATR.

    Price offsets are ATR-based so each child chases a different price level.
    Useful when you want passive fills spread across the current ATR range.

    Args:
        total_lot:   Total volume to slice
        base_price:  Reference price (e.g. current VWAP or mid)
        atr:         Current ATR value for offset calculation
        num_slices:  Number of child orders
        atr_spread:  Half-range = atr_spread * atr (orders placed from -range to +range)

    Returns:
        List of dicts: {price, lot, delay_seconds}  — send at delay_seconds apart
    """
    if total_lot <= 0:
        return []
    child_lot = round(total_lot / num_slices, 2)
    if child_lot < min_lot:
        child_lot = min_lot

    offsets = np.linspace(-atr_spread * atr, atr_spread * atr, num_slices)
    orders = []
    remaining = total_lot
    for i, offset in enumerate(offsets):
        lot = child_lot if i < num_slices - 1 else round(remaining, 2)
        orders.append({
            "price": round(base_price + offset, 2),
            "lot": max(lot, min_lot),
            "delay_seconds": i * 30,
        })
        remaining -= child_lot

    return orders


# ---------------------------------------------------------------------------
# TWAP order slicer
# ---------------------------------------------------------------------------
def twap_slice_orders(
    total_lot: float,
    base_price: float,
    twap_minutes: int = 10,
    num_slices: int = 5,
    side: int = 1,
    tick_size: float = 0.01,
    min_lot: float = 0.01,
) -> list[dict]:
    """Split a large order into equal child limit orders spaced evenly over time.

    Unlike VWAP slicer (which uses ATR price offsets), TWAP spaces children at
    fixed time intervals with equal size at the same limit price. This minimises
    market-impact by distributing participation over twap_minutes.

    For intraday XAU/USD the limit price is set 1 tick passive (inside spread)
    to guarantee limit-order economics:
      Long:  price = base_price - tick_size  (bid-side passive)
      Short: price = base_price + tick_size  (ask-side passive)

    Args:
        total_lot:    Total volume to execute
        base_price:   Current mid price
        twap_minutes: Window over which to distribute orders
        num_slices:   Number of child orders (evenly sized)
        side:         +1 = buy, -1 = sell
        tick_size:    Minimum price increment (default 0.01 for XAUUSD)

    Returns:
        List of dicts: {price, lot, delay_seconds, slice_index}
        delay_seconds is evenly spaced from 0 to twap_minutes*60.

    Usage:
        orders = twap_slice_orders(0.5, 2345.0, twap_minutes=10, num_slices=5, side=1)
        for o in orders:
            time.sleep(o["delay_seconds"])  # handled by MT5 EA timer
            send_limit_order(o["price"], o["lot"])
    """
    if total_lot <= 0 or num_slices <= 0:
        return []

    # Equal-size children; last slice absorbs rounding remainder
    child_lot = round(total_lot / num_slices, 2)
    if child_lot < min_lot:
        child_lot = min_lot

    # Passive limit price: 1 tick inside spread
    limit_price = round(base_price - side * tick_size, 2)

    # Evenly space across the window
    interval_seconds = int(twap_minutes * 60 / max(num_slices - 1, 1))

    orders = []
    remaining = total_lot
    for i in range(num_slices):
        lot = child_lot if i < num_slices - 1 else max(round(remaining, 2), min_lot)
        orders.append({
            "price": limit_price,
            "lot": lot,
            "delay_seconds": i * interval_seconds,
            "slice_index": i,
        })
        remaining = round(remaining - child_lot, 2)

    return orders
