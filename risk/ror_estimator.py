"""Regime-conditional Risk of Ruin (RoR) estimator.

Standard RoR assumes stationary win-rate and volatility — unrealistic for
markets that alternate between range and trend regimes.

This module uses a 2-regime Markov chain to simulate realistic equity paths:
  - Regime 0 (Range):  lower win-rate, tighter distribution
  - Regime 1 (Trend):  higher win-rate, wider distribution
  - Regime transitions: estimated from T-KAN regime history

Monte Carlo output:
  - RoR at configured ruin level (e.g. drawdown > MAX_DRAWDOWN_PCT)
  - Expected time-to-ruin (conditional on ruin occurring)
  - 5th/95th percentile equity curves

Usage:
    from risk.ror_estimator import RoREstimator
    ror = RoREstimator()
    result = ror.estimate(kelly_sizer, n_paths=5000, n_steps=1440)
    print(result["ror_pct"], result["expected_ttr_steps"])
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config import MAX_DRAWDOWN_PCT

logger = logging.getLogger("ror_estimator")


@dataclass
class RoRResult:
    ror_pct: float            # % of paths that hit ruin
    expected_ttr: float       # mean steps to ruin (NaN if ror=0)
    p5_final: float           # 5th pct equity at end (fraction of initial)
    p95_final: float          # 95th pct equity at end (fraction of initial)
    n_paths: int
    n_steps: int
    ruin_threshold: float     # fraction of initial equity (e.g. 0.90 for 10% DD)


class RoREstimator:
    """Monte Carlo RoR with 2-regime Markov chain.

    Regime transition matrix P (row = current regime, col = next):
      P[0,0] = p(stay range)   P[0,1] = p(range→trend)
      P[1,0] = p(trend→range)  P[1,1] = p(stay trend)

    Per-regime trade distribution derived from KellyPositionSizer history:
      Range regime:   win_rate × 0.85, avg_win × 0.80, avg_loss × 0.90
      Trend regime:   win_rate × 1.15, avg_win × 1.30, avg_loss × 1.10
      (scalars calibrated to observed regime performance differences)
    """

    # Markov regime transition defaults (calibrate from T-KAN history when enough data)
    DEFAULT_TRANSITION = np.array([
        [0.85, 0.15],   # range: 85% stay range, 15% → trend
        [0.20, 0.80],   # trend: 20% → range, 80% stay trend
    ])

    # Per-regime win-rate and P&L scalars relative to base Kelly estimates
    REGIME_WR_SCALAR     = {0: 0.85, 1: 1.15}   # range regime lower win-rate
    REGIME_WIN_SCALAR    = {0: 0.80, 1: 1.30}   # trend gives bigger wins
    REGIME_LOSS_SCALAR   = {0: 0.90, 1: 1.10}   # trend gives bigger losses too
    REGIME_KELLY_SCALAR  = {0: 0.50, 1: 1.00}   # range → half Kelly

    def __init__(
        self,
        ruin_threshold_pct: float = MAX_DRAWDOWN_PCT,
        transition_matrix: np.ndarray | None = None,
    ) -> None:
        self.ruin_frac = 1.0 - ruin_threshold_pct / 100.0
        self.P = transition_matrix if transition_matrix is not None else self.DEFAULT_TRANSITION

    # ── transition matrix calibration ────────────────────────────────────────

    def calibrate_transitions(self, regime_sequence: list[int]) -> None:
        """Estimate Markov transition matrix from observed regime labels.

        Args:
            regime_sequence: list of 0/1/-1 labels from T-KAN (−1 excluded).
        """
        seq = [r for r in regime_sequence if r in (0, 1)]
        if len(seq) < 10:
            return
        counts = np.zeros((2, 2))
        for i in range(len(seq) - 1):
            counts[seq[i], seq[i + 1]] += 1
        row_sums = counts.sum(axis=1, keepdims=True)
        row_sums = np.where(row_sums == 0, 1, row_sums)
        self.P = counts / row_sums
        logger.info("Calibrated regime transition matrix:\n%s", np.round(self.P, 3))

    # ── main estimator ────────────────────────────────────────────────────────

    def estimate(
        self,
        kelly_sizer,               # KellyPositionSizer instance
        n_paths: int = 5_000,
        n_steps: int = 1_440,      # 1 trading day of M1 bars
        initial_regime: int = -1,  # -1 = random from stationary dist
        seed: int | None = None,
    ) -> RoRResult:
        """Run Monte Carlo simulation.

        Args:
            kelly_sizer:    KellyPositionSizer to derive win-rate and P&L stats.
            n_paths:        Number of simulated equity paths.
            n_steps:        Steps per path (e.g. 1440 = 1 day of M1 ticks).
            initial_regime: Starting regime (−1 = sample from stationary dist).
            seed:           RNG seed for reproducibility.

        Returns:
            RoRResult with summary statistics.
        """
        rng = np.random.default_rng(seed)

        # Base Kelly stats from live history
        base_wr  = kelly_sizer.win_rate
        base_win = kelly_sizer.avg_win   / 100.0   # normalise to fraction of equity
        base_los = kelly_sizer.avg_loss  / 100.0

        if base_win <= 0:
            base_win = 0.01
        if base_los <= 0:
            base_los = 0.01

        # Stationary distribution (eigenvector of P^T for eigenvalue 1)
        stat_dist = self._stationary(self.P)

        # Starting regime
        if initial_regime not in (0, 1):
            initial_regime = int(rng.choice([0, 1], p=stat_dist))

        ruin_count   = 0
        ttr_list: list[float] = []
        final_equity = np.ones(n_paths)

        for path_i in range(n_paths):
            equity  = 1.0
            regime  = initial_regime
            ruined  = False

            for step in range(n_steps):
                # Regime-adjusted parameters
                wr  = np.clip(base_wr  * self.REGIME_WR_SCALAR[regime],  0.01, 0.99)
                win = base_win * self.REGIME_WIN_SCALAR[regime]
                los = base_los * self.REGIME_LOSS_SCALAR[regime]
                kf  = kelly_sizer.optimal_fraction() * self.REGIME_KELLY_SCALAR[regime]

                # One trade: risk kf fraction of equity
                risk_amt = equity * kf
                if rng.random() < wr:
                    equity += risk_amt * (win / base_win)
                else:
                    equity -= risk_amt * (los / base_los)

                if equity <= self.ruin_frac:
                    ruin_count += 1
                    ttr_list.append(step + 1)
                    ruined = True
                    break

                # Regime transition (one step of Markov chain)
                regime = int(rng.choice([0, 1], p=self.P[regime]))

            final_equity[path_i] = equity if not ruined else self.ruin_frac

        ror_pct      = ruin_count / n_paths * 100.0
        expected_ttr = float(np.mean(ttr_list)) if ttr_list else float("nan")
        p5_final     = float(np.percentile(final_equity, 5))
        p95_final    = float(np.percentile(final_equity, 95))

        result = RoRResult(
            ror_pct=round(ror_pct, 2),
            expected_ttr=round(expected_ttr, 1),
            p5_final=round(p5_final, 4),
            p95_final=round(p95_final, 4),
            n_paths=n_paths,
            n_steps=n_steps,
            ruin_threshold=self.ruin_frac,
        )
        logger.info(
            "RoR estimate: %.2f%% ruin in %d paths/%d steps | "
            "E[TTR]=%.1f steps | p5=%.4f p95=%.4f",
            ror_pct, n_paths, n_steps,
            expected_ttr, p5_final, p95_final,
        )
        return result

    # ── helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _stationary(P: np.ndarray) -> np.ndarray:
        """Compute stationary distribution of 2-state Markov chain."""
        p01 = P[0, 1]
        p10 = P[1, 0]
        denom = p01 + p10
        if denom < 1e-9:
            return np.array([0.5, 0.5])
        pi0 = p10 / denom
        pi1 = p01 / denom
        return np.array([pi0, pi1])
