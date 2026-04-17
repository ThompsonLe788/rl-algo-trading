"""Batch retrain T-KAN + PPO for all configured symbols.

Usage examples:
  python retrain_all.py                          # all symbols, live MT5 data
  python retrain_all.py --symbols XAUUSD EURUSD  # specific symbols
  python retrain_all.py --synthetic              # offline / no MT5
  python retrain_all.py --bars 100000 --timesteps 600000 --epochs 60
  python retrain_all.py --only tkan              # T-KAN only
  python retrain_all.py --only ppo               # PPO only
  python retrain_all.py --no-refresh             # use cached parquet
"""
# Force UTF-8 stdout so Windows CP1252 never crashes on any character
import io, sys
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "buffer"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import argparse
import time
import traceback
from pathlib import Path
from dataclasses import dataclass

sys.path.insert(0, str(Path(__file__).resolve().parent))


# ---------------------------------------------------------------------------
# Result tracking
# ---------------------------------------------------------------------------

@dataclass
class SymbolResult:
    symbol: str
    tkan_ok: bool = False
    ppo_ok: bool = False
    tkan_error: str = ""
    ppo_error: str = ""
    tkan_elapsed: float = 0.0
    ppo_elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return self.tkan_ok and self.ppo_ok

    def status_line(self, only: str) -> str:
        parts = [f"  {self.symbol:<10}"]
        if only in ("all", "tkan"):
            tkan_tag = "OK" if self.tkan_ok else f"FAIL({self.tkan_error[:40]})"
            parts.append(f"  T-KAN: {tkan_tag}  ({self.tkan_elapsed:.0f}s)")
        if only in ("all", "ppo"):
            ppo_tag = "OK" if self.ppo_ok else f"FAIL({self.ppo_error[:40]})"
            parts.append(f"  PPO: {ppo_tag}  ({self.ppo_elapsed:.0f}s)")
        return "".join(parts)


# ---------------------------------------------------------------------------
# Per-symbol helpers
# ---------------------------------------------------------------------------

def _load_data(symbol: str, bars: int, synthetic: bool, force_refresh: bool):
    """Load M1 bars for *symbol* (always M1 — never H1/H4 directly).

    H1 and H4 features are derived INSIDE build_feature_matrix() by
    resampling this M1 DataFrame. Minimum recommended: 50,000 M1 bars
    (~35 days) so H4 ATR warmup (14 x 4h = 56h) and H1 MA warmup (20h)
    have enough history.
    """
    if synthetic:
        from data.pipeline import generate_synthetic_data
        print(f"  [data] Generating {bars:,} synthetic M1 bars for {symbol}...")
        df = generate_synthetic_data(n_bars=bars)
    else:
        from data.pipeline import load_or_fetch
        print(f"  [data] Loading {bars:,} M1 bars for {symbol} "
              f"({'fresh MT5 fetch' if force_refresh else 'cache'})...")
        df = load_or_fetch(symbol=symbol, timeframe="M1",
                           num_bars=bars, force_refresh=force_refresh)

    # Verify data quality
    import pandas as pd
    has_datetime_idx = isinstance(df.index, pd.DatetimeIndex)
    h1_bars = len(df.resample("60min").last().dropna()) if has_datetime_idx else 0
    h4_bars = len(df.resample("240min").last().dropna()) if has_datetime_idx else 0
    print(f"  [data] {len(df):,} M1 bars | "
          f"H1: {h1_bars} bars | H4: {h4_bars} bars | "
          f"DatetimeIndex: {has_datetime_idx}")
    # H4 ATR warmup requires 14 × 4h = 56 H4 bars minimum.
    # Below this threshold, h4_atr_ratio will be noisy/zero — abort rather
    # than train on bad features and silently degrade the model.
    H4_MIN_BARS = 56
    if h4_bars < H4_MIN_BARS:
        if not synthetic and force_refresh is False:
            print(f"  [data] WARNING: only {h4_bars} H4 bars — retrying with force_refresh=True")
            df2 = _load_data(symbol, bars * 2, synthetic, force_refresh=True)
            h4_bars2 = len(df2.resample("240min").last().dropna()) if isinstance(df2.index, pd.DatetimeIndex) else 0
            if h4_bars2 >= H4_MIN_BARS:
                return df2
        raise ValueError(
            f"[{symbol}] Insufficient H4 data: {h4_bars} bars < {H4_MIN_BARS} required. "
            f"h4_atr_ratio will be noisy. Use --bars 100000 or check MT5 connection."
        )
    if not has_datetime_idx:
        print("  [data] WARNING: no DatetimeIndex — h1_slope and h4_atr_ratio "
              "will return zeros! MT5 data should always have DatetimeIndex.")
    return df


def _train_tkan(symbol: str, df, epochs: int) -> None:
    """Train T-KAN regime classifier and save model."""
    import numpy as np
    from ai_models.features import build_feature_matrix
    from ai_models.regime_tkan import label_regimes, train_tkan
    from config import TKAN_SEQ_LEN, TKAN_INPUT_DIM, MODEL_DIR

    print(f"  [tkan] Building features...")
    feats = build_feature_matrix(df).fillna(0.0)
    labels = label_regimes(df)

    X, Y = [], []
    for i in range(TKAN_SEQ_LEN, len(feats)):
        seq = feats.iloc[i - TKAN_SEQ_LEN:i].values[:, :TKAN_INPUT_DIM]
        X.append(seq)
        Y.append(labels[i])

    X = np.array(X, dtype=np.float32)
    Y = np.array(Y, dtype=np.int64)

    n_range = int((Y == 0).sum())
    n_trend = int((Y == 1).sum())
    print(f"  [tkan] {len(X):,} sequences — Range={n_range:,} ({n_range/len(X):.1%}) "
          f"Trend={n_trend:,} ({n_trend/len(X):.1%})")

    save_path = MODEL_DIR / f"regime_tkan_{symbol.lower()}.pt"
    train_tkan(X, Y, epochs=epochs, save_path=save_path)
    print(f"  [tkan] Saved: {save_path}")


def _train_ppo(symbol: str, df, timesteps: int) -> None:
    """Train PPO agent and save model."""
    from ai_models.rl_agent import train_ppo
    from config import MODEL_DIR

    print(f"  [ppo]  Training on {len(df):,} M1 bars, {timesteps:,} timesteps...")
    train_ppo(df, total_timesteps=timesteps, symbol=symbol)
    save_path = MODEL_DIR / f"ppo_{symbol.lower()}.zip"
    print(f"  [ppo]  Saved: {save_path}")


# ---------------------------------------------------------------------------
# Main batch loop
# ---------------------------------------------------------------------------

def retrain_all(
    symbols: list[str],
    bars: int,
    timesteps: int,
    epochs: int,
    only: str,
    synthetic: bool,
    force_refresh: bool,
) -> list[SymbolResult]:
    results: list[SymbolResult] = []
    total = len(symbols)

    for idx, sym in enumerate(symbols, 1):
        print(f"\n{'='*60}")
        print(f"[{idx}/{total}] {sym}")
        print(f"{'='*60}")
        res = SymbolResult(symbol=sym)

        # Load M1 data (H1/H4 derived internally by feature pipeline)
        try:
            df = _load_data(sym, bars, synthetic, force_refresh)
        except Exception as e:
            err = str(e)
            print(f"  [data] ERROR: {err}")
            traceback.print_exc()
            res.tkan_error = res.ppo_error = f"data: {err}"
            results.append(res)
            continue

        # T-KAN
        if only in ("all", "tkan"):
            t0 = time.time()
            try:
                _train_tkan(sym, df, epochs=epochs)
                res.tkan_ok = True
            except Exception as e:
                res.tkan_error = str(e)
                print(f"  [tkan] ERROR: {e}")
                traceback.print_exc()
            res.tkan_elapsed = time.time() - t0
            print(f"  [tkan] {'OK' if res.tkan_ok else 'FAILED'}  ({res.tkan_elapsed:.0f}s)")
        else:
            res.tkan_ok = True

        # PPO
        if only in ("all", "ppo"):
            t0 = time.time()
            try:
                _train_ppo(sym, df, timesteps=timesteps)
                res.ppo_ok = True
            except Exception as e:
                res.ppo_error = str(e)
                print(f"  [ppo]  ERROR: {e}")
                traceback.print_exc()
            res.ppo_elapsed = time.time() - t0
            print(f"  [ppo]  {'OK' if res.ppo_ok else 'FAILED'}  ({res.ppo_elapsed:.0f}s)")
        else:
            res.ppo_ok = True

        results.append(res)

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    from config import SYMBOL_CONFIGS
    all_symbols = list(SYMBOL_CONFIGS.keys())

    parser = argparse.ArgumentParser(
        description="Batch retrain T-KAN + PPO for all configured symbols"
    )
    parser.add_argument("--symbols", nargs="+", default=all_symbols, metavar="SYM")
    parser.add_argument("--bars", type=int, default=100_000,
                        help="M1 bars per symbol (default: 100000 = ~70 days, "
                             "gives ~1650 H1 bars and ~420 H4 bars)")
    parser.add_argument("--timesteps", type=int, default=500_000)
    parser.add_argument("--epochs", type=int, default=50)
    parser.add_argument("--only", choices=["all", "tkan", "ppo"], default="all")
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--no-refresh", action="store_true",
                        help="Use cached parquet (skip MT5 fetch)")
    args = parser.parse_args()

    symbols = [s.upper() for s in args.symbols]
    force_refresh = not args.no_refresh

    print("=" * 60)
    print("Batch Retrain - XAU ATS")
    print("=" * 60)
    print(f"  Symbols   : {', '.join(symbols)}")
    print(f"  Data mode : {'synthetic' if args.synthetic else ('cache' if not force_refresh else 'live MT5 fetch')}")
    print(f"  M1 bars   : {args.bars:,}  (H1 ~{args.bars//60:,} bars, H4 ~{args.bars//240:,} bars)")
    print(f"  PPO steps : {args.timesteps:,}")
    print(f"  T-KAN ep  : {args.epochs}")
    print(f"  Train     : {args.only}")
    print("=" * 60)

    wall_start = time.time()
    results = retrain_all(
        symbols=symbols,
        bars=args.bars,
        timesteps=args.timesteps,
        epochs=args.epochs,
        only=args.only,
        synthetic=args.synthetic,
        force_refresh=force_refresh,
    )

    wall_elapsed = time.time() - wall_start
    ok_count   = sum(1 for r in results if r.ok)
    fail_count = len(results) - ok_count

    print(f"\n{'='*60}")
    print(f"Retrain Summary  ({wall_elapsed/60:.1f} min total)")
    print(f"{'='*60}")
    for r in results:
        marker = "[OK]" if r.ok else "[FAIL]"
        print(f"  {marker}  {r.status_line(args.only)}")
    print(f"\n  Passed: {ok_count}/{len(results)}   Failed: {fail_count}/{len(results)}")

    if fail_count:
        print("\nFailed symbols:")
        for r in results:
            if not r.ok:
                if r.tkan_error:
                    print(f"  {r.symbol}  T-KAN: {r.tkan_error}")
                if r.ppo_error:
                    print(f"  {r.symbol}  PPO  : {r.ppo_error}")
        sys.exit(1)


if __name__ == "__main__":
    main()
