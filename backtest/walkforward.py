"""Walk-forward optimization with TimeSeriesSplit.

Trains PPO on rolling windows, evaluates on out-of-sample,
collects per-fold metrics for robustness assessment.
Includes fill-level TCA recording for slippage analysis.
"""
import numpy as np
import pandas as pd
from sklearn.model_selection import TimeSeriesSplit
from pathlib import Path
from dataclasses import dataclass, field

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from ai_models.rl_agent import train_ppo, XauIntradayEnv, calc_execution_price
from backtest.tca import run_tca, TCAReport
from stable_baselines3 import PPO


@dataclass
class FoldMetrics:
    fold: int
    sharpe: float
    total_return: float
    max_drawdown: float
    win_rate: float
    num_trades: int
    avg_slippage_bps: float = 0.0
    tca: TCAReport | None = None


@dataclass
class WalkForwardResult:
    folds: list[FoldMetrics] = field(default_factory=list)

    @property
    def avg_sharpe(self) -> float:
        return np.mean([f.sharpe for f in self.folds])

    @property
    def avg_win_rate(self) -> float:
        return np.mean([f.win_rate for f in self.folds])

    def summary(self) -> str:
        lines = ["Walk-Forward Results", "=" * 50]
        for f in self.folds:
            tca_str = f"  Slip={f.tca.avg_slippage_bps:.2f}bps  IS={f.tca.implementation_shortfall_bps:.2f}bps" if f.tca else ""
            lines.append(
                f"Fold {f.fold}: Sharpe={f.sharpe:.3f}  Return={f.total_return:.4f}  "
                f"MDD={f.max_drawdown:.4f}  WR={f.win_rate:.3f}  "
                f"Trades={f.num_trades}{tca_str}"
            )
        lines.append("-" * 50)
        avg_slip = np.mean([f.tca.avg_slippage_bps for f in self.folds if f.tca]) if any(f.tca for f in self.folds) else 0.0
        lines.append(f"Avg Sharpe: {self.avg_sharpe:.3f}  Avg WR: {self.avg_win_rate:.3f}  Avg Slippage: {avg_slip:.2f}bps")
        return "\n".join(lines)


def evaluate_agent(model: PPO, test_df: pd.DataFrame) -> FoldMetrics:
    """Evaluate a trained PPO agent on test data.

    Records fill-level data for TCA analysis:
      arrival_price = mid at signal bar (decision price)
      fill_price    = mid + 0.5*spread (simulated limit fill with queue risk)
    """
    env = XauIntradayEnv(test_df)
    obs, _ = env.reset()

    returns = []
    equity_curve = [1.0]
    trades = 0
    wins = 0
    fills = []          # list of dicts for TCA

    prev_position = 0

    while True:
        bar_idx = env.i
        mid_col = "mid" if "mid" in test_df.columns else "close"
        arrival = float(test_df.iloc[min(bar_idx, len(test_df) - 1)][mid_col])

        action, _ = model.predict(obs, deterministic=True)
        obs, reward, done, truncated, info = env.step(int(action))
        pnl = info.get("pnl", 0.0)
        cur_position = info.get("position", 0)
        returns.append(pnl)
        equity_curve.append(equity_curve[-1] + pnl)

        # Detect trade opening/closing for fill records
        if action in (1, 2) and prev_position == 0 and cur_position != 0:
            side = 1 if action == 1 else -1
            # Realistic fill price via square-root impact model (⑫)
            fill_price = calc_execution_price(arrival, side, lot=0.01)
            fills.append({
                "fill_price": fill_price,
                "arrival_price": arrival,
                "decision_price": arrival,
                "side": side,
                "lot": 0.01,
                "bar_index": bar_idx,
            })
            trades += 1

        if pnl != 0 and cur_position == 0 and prev_position != 0:
            if pnl > 0:
                wins += 1

        prev_position = cur_position

        if done or truncated:
            break

    returns = np.array(returns)
    equity = np.array(equity_curve)

    # Sharpe (annualized assuming 1-min bars, 252 trading days)
    sharpe = returns.mean() / (returns.std() + 1e-9) * np.sqrt(252 * 1440)

    # Max drawdown
    peak = np.maximum.accumulate(equity)
    dd = (peak - equity) / (peak + 1e-9)
    max_dd = float(dd.max())

    win_rate = wins / max(trades, 1)

    # TCA analysis
    fills_df = pd.DataFrame(fills) if fills else pd.DataFrame(
        columns=["fill_price", "arrival_price", "decision_price", "side", "lot", "bar_index"]
    )
    tca_report = run_tca(fills_df, test_df)

    return FoldMetrics(
        fold=0,
        sharpe=float(sharpe),
        total_return=float(equity[-1] - 1.0),
        max_drawdown=max_dd,
        win_rate=win_rate,
        num_trades=trades,
        avg_slippage_bps=tca_report.avg_slippage_bps,
        tca=tca_report,
    )


def walk_forward(
    tick_df: pd.DataFrame,
    regime_model=None,
    n_splits: int = 6,
    test_days: int = 20,
    bars_per_day: int = 1440,
    timesteps_per_fold: int = 200_000,
) -> WalkForwardResult:
    """Run walk-forward optimization.

    Args:
        tick_df: Full historical DataFrame
        regime_model: Trained T-KAN (optional)
        n_splits: Number of train/test folds
        test_days: Number of days per test set
        bars_per_day: Bars per trading day (1440 for 1-min)
        timesteps_per_fold: PPO training steps per fold
    """
    test_size = test_days * bars_per_day
    tscv = TimeSeriesSplit(n_splits=n_splits, test_size=test_size)

    result = WalkForwardResult()

    for fold_idx, (train_idx, test_idx) in enumerate(tscv.split(tick_df)):
        print(f"\n{'='*50}")
        print(f"Fold {fold_idx + 1}/{n_splits}")
        print(f"Train: {len(train_idx)} bars, Test: {len(test_idx)} bars")

        train_df = tick_df.iloc[train_idx].reset_index(drop=True)
        test_df = tick_df.iloc[test_idx].reset_index(drop=True)

        # Train PPO on this fold
        model = train_ppo(
            train_df,
            regime_model=regime_model,
            total_timesteps=timesteps_per_fold,
        )

        # Evaluate on out-of-sample
        metrics = evaluate_agent(model, test_df)
        metrics.fold = fold_idx + 1
        result.folds.append(metrics)

        print(f"  Sharpe: {metrics.sharpe:.3f}  Return: {metrics.total_return:.4f}  "
              f"MDD: {metrics.max_drawdown:.4f}  Trades: {metrics.num_trades}")

    print(f"\n{result.summary()}")
    return result


# ---------------------------------------------------------------------------
# Look-ahead bias detector
# ---------------------------------------------------------------------------
def check_lookahead_bias(
    df: pd.DataFrame,
    feature_fn=None,
    check_indices: list[int] | None = None,
    atol: float = 1e-6,
) -> None:
    """Assert that no feature at time t uses data from t+1 onwards.

    Method: for each index T in check_indices, corrupt all OHLCV rows after T
    with sentinel values (price=99999, volume=0), recompute features on the
    corrupted dataset, then assert feature[T] is unchanged.

    A mismatch at any T proves that the feature pipeline has look-ahead bias.

    Args:
        df:            Full OHLCV DataFrame (DatetimeIndex)
        feature_fn:    Callable df → DataFrame of features.
                       Defaults to ai_models.features.build_feature_matrix.
        check_indices: List of bar indices to probe.
                       Defaults to [200, 500, 1000, 2000] (capped at len-1).
        atol:          Absolute tolerance for floating-point equality.

    Raises:
        AssertionError: If look-ahead bias is detected at any index, with a
                        detailed diff showing which feature columns changed.

    Example:
        from backtest.walkforward import check_lookahead_bias
        from data.pipeline import load_or_fetch
        df = load_or_fetch("xau_m1")
        check_lookahead_bias(df)   # raises if bias found
        print("No look-ahead bias detected.")
    """
    if feature_fn is None:
        from ai_models.features import build_feature_matrix
        feature_fn = build_feature_matrix

    n = len(df)
    if check_indices is None:
        check_indices = [200, 500, 1000, 2000]
    check_indices = [t for t in check_indices if 0 < t < n - 1]

    if not check_indices:
        raise ValueError(f"No valid check indices in range [1, {n-2}]")

    # Precompute features on original data
    feats_orig = feature_fn(df)

    price_cols = [c for c in ["open", "high", "low", "close"] if c in df.columns]
    vol_cols   = [c for c in ["volume"] if c in df.columns]

    violations = []

    for T in check_indices:
        df_corrupt = df.copy()

        # Corrupt everything after T with sentinel values
        for col in price_cols:
            df_corrupt.iloc[T + 1:, df_corrupt.columns.get_loc(col)] = 99_999.0
        for col in vol_cols:
            df_corrupt.iloc[T + 1:, df_corrupt.columns.get_loc(col)] = 0.0
        if "bid" in df_corrupt.columns:
            df_corrupt.iloc[T + 1:, df_corrupt.columns.get_loc("bid")] = 99_999.0
        if "ask" in df_corrupt.columns:
            df_corrupt.iloc[T + 1:, df_corrupt.columns.get_loc("ask")] = 99_999.0

        feats_corrupt = feature_fn(df_corrupt)

        row_orig    = np.nan_to_num(feats_orig.iloc[T].values.astype(float))
        row_corrupt = np.nan_to_num(feats_corrupt.iloc[T].values.astype(float))

        diff = np.abs(row_orig - row_corrupt)
        bad_mask = diff > atol

        if bad_mask.any():
            bad_cols = feats_orig.columns[bad_mask].tolist()
            bad_diffs = diff[bad_mask].tolist()
            violations.append(
                f"  T={T}: {len(bad_cols)} feature(s) changed when future data corrupted:\n"
                + "\n".join(
                    f"    [{c}]  orig={row_orig[feats_orig.columns.get_loc(c)]:.6f}"
                    f"  corrupt={row_corrupt[feats_orig.columns.get_loc(c)]:.6f}"
                    f"  diff={d:.2e}"
                    for c, d in zip(bad_cols, bad_diffs)
                )
            )

    if violations:
        raise AssertionError(
            "LOOK-AHEAD BIAS DETECTED in feature pipeline!\n"
            + "\n".join(violations)
        )

    print(f"check_lookahead_bias: PASS — {len(check_indices)} probe indices, no bias found.")
