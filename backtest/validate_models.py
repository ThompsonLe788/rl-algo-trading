"""OOS model validation — evaluate existing PPO models on multiple time windows.

No retraining. Uses the current checkpoint models.
Splits last N bars into 5 non-overlapping OOS windows and reports Sharpe,
win rate, max drawdown per window.

Usage:
  python backtest/validate_models.py --symbols XAUUSD GBPUSD BTCUSD
  python backtest/validate_models.py --bars 50000 --windows 5
"""
import io
import argparse
import sys
from pathlib import Path

# Force UTF-8 stdout so Windows CP1252 never crashes on special characters
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "buffer"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.pipeline import load_or_fetch
from ai_models.rl_agent import load_ppo
from backtest.walkforward import evaluate_agent, FoldMetrics
from config import SYMBOL_CONFIGS as SYMBOLS_CONFIG

# Pass criteria
# Note: env equity starts at 1.0 and accumulates raw price PnL (unnormalized),
# so MDD > 1.0 is common and meaningless. Use Sharpe + Return direction + WR.
MIN_SHARPE       = 1.0    # annualised Sharpe on OOS window
MIN_WIN_RATE     = 0.35   # cumulative per-trade win rate (fixed calculation)
MIN_POS_WINDOWS  = 0.60   # at least 60% of windows must have positive return


def _run_episode(model, test_df):
    """Run one OOS episode; return (sharpe, total_return, mdd, trades, win_rate).
    Win rate computed from cumulative per-trade PnL (not incremental bar pnl).
    """
    from ai_models.rl_agent import XauIntradayEnv, calc_execution_price
    import numpy as np

    env = XauIntradayEnv(test_df)
    obs, _ = env.reset()

    bar_returns = []
    equity = [1.0]
    entries = 0
    trade_pnl_accum = 0.0  # cumulative PnL for current open trade
    wins = 0
    trade_results = []

    prev_pos = 0

    while True:
        action, _ = model.predict(obs, deterministic=True)
        obs, reward, done, truncated, info = env.step(int(action))
        pnl_step  = info.get("pnl", 0.0)
        cur_pos   = info.get("position", 0)

        bar_returns.append(pnl_step)
        equity.append(equity[-1] + pnl_step)

        # Accumulate PnL while in a position
        if prev_pos != 0:
            trade_pnl_accum += pnl_step

        # New entry
        if cur_pos != 0 and prev_pos == 0:
            entries += 1
            trade_pnl_accum = 0.0

        # Position closed — record cumulative trade PnL
        if cur_pos == 0 and prev_pos != 0:
            trade_results.append(trade_pnl_accum)
            trade_pnl_accum = 0.0

        prev_pos = cur_pos
        if done or truncated:
            break

    arr = np.array(bar_returns)
    eq  = np.array(equity)
    sharpe = arr.mean() / (arr.std() + 1e-9) * np.sqrt(252 * 1440)
    peak   = np.maximum.accumulate(eq)
    mdd    = float(((peak - eq) / (peak + 1e-9)).max())
    wr     = sum(1 for p in trade_results if p > 0) / max(len(trade_results), 1)
    return float(sharpe), float(eq[-1] - 1.0), mdd, entries, wr


def validate(symbol: str, bars: int = 60_000, n_windows: int = 5) -> bool:
    print(f"\n{'='*60}")
    print(f"Validating {symbol}  ({bars} bars, {n_windows} OOS windows)")
    print(f"{'='*60}")

    # Load model
    try:
        model = load_ppo(symbol=symbol)
        print(f"Model loaded: ai_models/checkpoints/ppo_{symbol.lower()}.zip")
    except Exception as e:
        print(f"FAIL: cannot load model — {e}")
        return False

    # Fetch data (cached parquet preferred)
    try:
        import MetaTrader5 as mt5
        mt5.initialize()
        df = load_or_fetch(symbol, num_bars=bars, timeframe="M1", force_refresh=False)
        mt5.shutdown()
    except Exception as e:
        print(f"FAIL: cannot fetch data — {e}")
        return False

    if len(df) < bars * 0.5:
        print(f"WARN: only {len(df)} bars available (expected {bars})")

    # Split into windows
    window_size = len(df) // (n_windows + 1)
    window_data = []

    for i in range(n_windows):
        start = (i + 1) * window_size
        end   = start + window_size
        if end > len(df):
            break
        test_df = df.iloc[start:end].reset_index(drop=True)
        sharpe, ret, mdd, trades, wr = _run_episode(model, test_df)
        ok = sharpe >= MIN_SHARPE and ret > 0 and wr >= MIN_WIN_RATE
        window_data.append((sharpe, ret, mdd, trades, wr, ok))
        tag = "OK" if ok else "XX"
        print(
            f"  Window {i+1}: Sharpe={sharpe:7.3f}  Return={ret:+.4f}"
            f"  MDD={mdd:.3f}  WR={wr:.3f}  Trades={trades:4d}  [{tag}]"
        )

    if not window_data:
        print("FAIL: no evaluation windows")
        return False

    avg_sharpe   = float(np.mean([w[0] for w in window_data]))
    avg_wr       = float(np.mean([w[4] for w in window_data]))
    pos_windows  = sum(1 for w in window_data if w[1] > 0) / len(window_data)
    n_ok         = sum(1 for w in window_data if w[5])

    print(f"\n  Avg Sharpe    : {avg_sharpe:.3f}  (min {MIN_SHARPE})")
    print(f"  Avg WR        : {avg_wr:.3f}  (min {MIN_WIN_RATE})")
    print(f"  Positive wins : {pos_windows:.0%}  (min {MIN_POS_WINDOWS:.0%} windows)")
    print(f"  Windows OK    : {n_ok}/{len(window_data)}")

    passed = (avg_sharpe >= MIN_SHARPE
              and avg_wr >= MIN_WIN_RATE
              and pos_windows >= MIN_POS_WINDOWS)
    verdict = "PASS - model approved for live trading" if passed else "FAIL - model needs retraining"
    print(f"\n  Verdict: {verdict}")
    return passed


def main():
    parser = argparse.ArgumentParser(description="OOS model validation")
    parser.add_argument("--symbols", nargs="+", default=list(SYMBOLS_CONFIG.keys()),
                        metavar="SYM")
    parser.add_argument("--bars",    type=int, default=60_000,
                        help="M1 bars to fetch per symbol (default 60000 ≈ 42 days)")
    parser.add_argument("--windows", type=int, default=5,
                        help="Number of OOS evaluation windows (default 5)")
    args = parser.parse_args()

    results = {}
    for sym in args.symbols:
        results[sym] = validate(sym, bars=args.bars, n_windows=args.windows)

    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    for sym, ok in results.items():
        print(f"  {sym:<10} {'PASS ✓' if ok else 'FAIL ✗'}")

    failed = [s for s, ok in results.items() if not ok]
    if failed:
        print(f"\nSymbols needing retrain: {', '.join(failed)}")
        print(f"Run: python retrain_all.py --symbols {' '.join(failed)} --bars 100000 --timesteps 500000")
        sys.exit(1)
    else:
        print("\nAll models approved for live trading.")


if __name__ == "__main__":
    main()
