"""Multi-Symbol ATS — Main entry points.

Usage examples:
  python main.py train-tkan --symbol XAUUSD --synthetic
  python main.py train-ppo  --symbol EURUSD --bars 50000
  python main.py backtest   --symbol GBPUSD --folds 6
  python main.py live       --symbol XAUUSD
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _load_df(args):
    """Load or generate training data based on CLI args (shared by all train/backtest commands)."""
    from data.pipeline import load_or_fetch, generate_synthetic_data
    if getattr(args, "synthetic", False):
        return generate_synthetic_data(n_bars=args.bars)
    return load_or_fetch(symbol=args.symbol.upper(), num_bars=args.bars)


def cmd_train_tkan(args):
    """Train T-KAN regime classifier for a given symbol."""
    import numpy as np
    from ai_models.features import build_feature_matrix
    from ai_models.regime_tkan import TKAN, label_regimes, train_tkan
    from config import TKAN_SEQ_LEN, TKAN_INPUT_DIM, MODEL_DIR

    sym = args.symbol.upper()
    df = _load_df(args)

    feats = build_feature_matrix(df).fillna(0.0)
    labels = label_regimes(df)

    X, Y = [], []
    for i in range(TKAN_SEQ_LEN, len(feats)):
        seq = feats.iloc[i - TKAN_SEQ_LEN:i].values[:, :TKAN_INPUT_DIM]
        X.append(seq)
        Y.append(labels[i])

    X = np.array(X, dtype=np.float32)
    Y = np.array(Y, dtype=np.int64)
    save_path = MODEL_DIR / f"regime_tkan_{sym.lower()}.pt"
    print(f"[{sym}] Training T-KAN on {len(X)} sequences → {save_path}")
    train_tkan(X, Y, epochs=args.epochs, save_path=save_path)


def cmd_train_ppo(args):
    """Train PPO agent for a given symbol."""
    from ai_models.rl_agent import train_ppo

    sym = args.symbol.upper()
    df = _load_df(args)

    print(f"[{sym}] Training PPO on {len(df)} bars...")
    train_ppo(df, total_timesteps=args.timesteps, symbol=sym)


def cmd_train_sac(args):
    """Train SAC agent (off-policy, continuous action) for a given symbol."""
    from ai_models.rl_agent import train_sac

    sym = args.symbol.upper()
    df = _load_df(args)

    print(f"[{sym}] Training SAC on {len(df)} bars...")
    train_sac(df, total_timesteps=args.timesteps, symbol=sym)


def cmd_backtest(args):
    """Run walk-forward backtest for a given symbol."""
    from backtest.walkforward import walk_forward

    sym = args.symbol.upper()
    df = _load_df(args)

    result = walk_forward(df, n_splits=args.folds, test_days=args.test_days)
    print(result.summary())


def cmd_live(args):
    """Run live signal server for a given symbol (auto-detect from --symbol)."""
    from ai_models.rl_agent import load_ppo
    from mt5_bridge.signal_server import run_live_loop
    from data.pipeline import LiveTickStream
    import MetaTrader5 as mt5

    sym = args.symbol.upper()
    model = load_ppo(symbol=sym)

    if not mt5.initialize():
        print(f"MT5 init failed: {mt5.last_error()}")
        return

    def account_info():
        info = mt5.account_info()
        if info is None:
            return 0.0, 0.0
        return info.equity, info.balance

    tick_stream = LiveTickStream(symbol=sym)
    print(f"[{sym}] Starting live signal server...")
    run_live_loop(model, None, tick_stream, account_info, symbol=sym)


def cmd_multi_live(args):
    """Watch MT5 open charts via ATS_Panel registration files and auto-start a
    signal-server thread per symbol. No --symbol needed — charts self-register."""
    from mt5_bridge.multi_runner import run_multi_live
    run_multi_live()


def main():
    parser = argparse.ArgumentParser(
        description="Multi-Symbol ATS — pass --symbol for any instrument"
    )
    sub = parser.add_subparsers(dest="command")

    # Shared --symbol argument added to all subcommands below
    def _add_symbol(p):
        p.add_argument(
            "--symbol", default="XAUUSD",
            help="Trading symbol (default: XAUUSD). EA auto-detects via _Symbol.",
        )

    # Train T-KAN
    p = sub.add_parser("train-tkan", help="Train T-KAN regime classifier")
    _add_symbol(p)
    p.add_argument("--bars", type=int, default=50000)
    p.add_argument("--epochs", type=int, default=50)
    p.add_argument("--synthetic", action="store_true")
    p.set_defaults(func=cmd_train_tkan)

    # Train PPO
    p = sub.add_parser("train-ppo", help="Train PPO agent")
    _add_symbol(p)
    p.add_argument("--bars", type=int, default=50000)
    p.add_argument("--timesteps", type=int, default=500000)
    p.add_argument("--synthetic", action="store_true")
    p.set_defaults(func=cmd_train_ppo)

    # Train SAC
    p = sub.add_parser("train-sac", help="Train SAC agent (off-policy)")
    _add_symbol(p)
    p.add_argument("--bars", type=int, default=50000)
    p.add_argument("--timesteps", type=int, default=500000)
    p.add_argument("--synthetic", action="store_true")
    p.set_defaults(func=cmd_train_sac)

    # Backtest
    p = sub.add_parser("backtest", help="Walk-forward backtest")
    _add_symbol(p)
    p.add_argument("--bars", type=int, default=100000)
    p.add_argument("--folds", type=int, default=6)
    p.add_argument("--test-days", type=int, default=20)
    p.add_argument("--synthetic", action="store_true")
    p.set_defaults(func=cmd_backtest)

    # Live (single symbol)
    p = sub.add_parser("live", help="Run live signal server")
    _add_symbol(p)
    p.set_defaults(func=cmd_live)

    # Multi-live (auto-detect from open MT5 charts via ATS_Panel)
    p = sub.add_parser(
        "multi-live",
        help="Auto-detect open MT5 charts and run signal server per symbol",
    )
    p.set_defaults(func=cmd_multi_live)

    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        return
    args.func(args)


if __name__ == "__main__":
    main()
