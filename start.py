"""ATS System Startup Script.

Performs pre-flight checks, trains models if missing,
then launches signal server + Streamlit dashboard as subprocesses.

Usage:
  python start.py                    # XAUUSD, use real MT5 data if available
  python start.py --symbol EURUSD    # other symbol
  python start.py --synthetic        # force synthetic data for quick start
  python start.py --dashboard-only   # only start Streamlit (no signal server)
"""
import argparse
import subprocess
import sys
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from config import MODEL_DIR, DATA_DIR, LOG_DIR, LIVE_STATE_PATH


# ---------------------------------------------------------------------------
# ANSI colours (works in Windows Terminal)
# ---------------------------------------------------------------------------
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

ok   = lambda s: print(f"{GREEN}  [OK]{RESET}  {s}")
warn = lambda s: print(f"{YELLOW} [WARN]{RESET} {s}")
fail = lambda s: print(f"{RED} [FAIL]{RESET} {s}")
info = lambda s: print(f"       {s}")


# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

def check_deps() -> bool:
    required = ["zmq", "stable_baselines3", "gymnasium", "torch",
                "pandas", "numpy", "streamlit"]
    missing = []
    for pkg in required:
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
    if missing:
        fail(f"Missing packages: {', '.join(missing)}")
        info("Run: pip install -r requirements.txt")
        return False
    ok("All Python dependencies installed")
    return True


def check_mt5() -> bool:
    try:
        import MetaTrader5 as mt5
        if not mt5.initialize():
            warn(f"MT5 terminal not connected: {mt5.last_error()}")
            info("Start MetaTrader 5 terminal and try again, or use --synthetic")
            return False
        info_mt5 = mt5.terminal_info()
        mt5.shutdown()
        ok(f"MetaTrader 5 connected ({info_mt5.name})")
        return True
    except ImportError:
        warn("MetaTrader5 package not installed — will use synthetic data")
        return False


def check_or_migrate_model(symbol: str) -> bool:
    """Return True if usable model found or migrated."""
    new_path = MODEL_DIR / f"ppo_{symbol.lower()}.zip"
    if new_path.exists():
        ok(f"Model found: {new_path.name}")
        return True

    # Try to migrate old hardcoded names
    old_candidates = [
        MODEL_DIR / "ppo_xau.zip",
        MODEL_DIR / "ppo_xauusd.zip",
        MODEL_DIR / f"ppo_{symbol[:3].lower()}.zip",
    ]
    for old in old_candidates:
        if old.exists():
            shutil.copy2(old, new_path)
            ok(f"Migrated {old.name} → {new_path.name}")
            return True

    warn(f"No PPO model for {symbol} — will train on synthetic data")
    return False


# ---------------------------------------------------------------------------
# Model training (synthetic)
# ---------------------------------------------------------------------------

def train_quick(symbol: str):
    print(f"\n{BOLD}Training PPO on synthetic data for {symbol}...{RESET}")
    print("  (50 000 bars, 200 000 timesteps — takes ~2-4 min)")
    result = subprocess.run(
        [sys.executable, "main.py", "train-ppo",
         "--symbol", symbol,
         "--synthetic",
         "--bars", "50000",
         "--timesteps", "200000"],
        cwd=ROOT,
    )
    if result.returncode != 0:
        fail("Training failed")
        sys.exit(1)
    ok(f"PPO model trained for {symbol}")


# ---------------------------------------------------------------------------
# Launch subprocesses
# ---------------------------------------------------------------------------

def start_signal_server(symbol: str) -> subprocess.Popen:
    print(f"\n{BOLD}Starting signal server [{symbol}]...{RESET}")
    proc = subprocess.Popen(
        [sys.executable, "main.py", "live", "--symbol", symbol],
        cwd=ROOT,
    )
    ok(f"Signal server PID {proc.pid} — ZMQ PUB on tcp://127.0.0.1:5555")
    return proc


def start_dashboard() -> subprocess.Popen:
    print(f"\n{BOLD}Starting Streamlit dashboard...{RESET}")
    proc = subprocess.Popen(
        ["streamlit", "run", "dashboard/app.py",
         "--server.port", "8501",
         "--server.headless", "true"],
        cwd=ROOT,
    )
    ok(f"Streamlit PID {proc.pid} — http://localhost:8501")
    return proc


def start_telegram_bot() -> subprocess.Popen | None:
    import os
    token = os.environ.get("TELEGRAM_TOKEN", "")
    if not token:
        warn("TELEGRAM_TOKEN not set — Telegram bot not started")
        info("Set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID env vars to enable")
        return None
    print(f"\n{BOLD}Starting Telegram bot...{RESET}")
    proc = subprocess.Popen(
        [sys.executable, "dashboard/telegram_bot.py"],
        cwd=ROOT,
    )
    ok(f"Telegram bot PID {proc.pid}")
    return proc


# ---------------------------------------------------------------------------
# Print MT5 EA setup instructions
# ---------------------------------------------------------------------------

def print_mt5_instructions(symbol: str):
    print(f"""
{BOLD}MT5 Setup (manual steps){RESET}
  1. Open {symbol} chart in MetaTrader 5
  2. Drag {BOLD}XauDayTrader{RESET} EA onto the chart
     - ZmqAddress: tcp://127.0.0.1:5555
     - ZmqTopic:   (leave blank — auto-uses {symbol})
     - MagicNumber: 20250411
  3. Drag {BOLD}ATS_Panel{RESET} indicator onto the same chart
     - Panel will appear top-right showing live state

  Signal file fallback: {symbol.lower()}_signal.json in MT5 Common Files
""")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="ATS System Startup")
    parser.add_argument("--symbol",         default="XAUUSD")
    parser.add_argument("--synthetic",      action="store_true",
                        help="Force synthetic data (skip MT5 live feed)")
    parser.add_argument("--dashboard-only", action="store_true",
                        help="Start only Streamlit (no signal server)")
    args = parser.parse_args()

    symbol = args.symbol.upper()

    print(f"\n{BOLD}{'='*50}")
    print(f"  ATS System Startup — {symbol}")
    print(f"{'='*50}{RESET}\n")

    # Dependency checks
    if not check_deps():
        sys.exit(1)

    mt5_ok = check_mt5()

    # Model check / train
    if not args.dashboard_only:
        model_ok = check_or_migrate_model(symbol)
        if not model_ok:
            train_quick(symbol)

    print()

    procs = []

    if not args.dashboard_only:
        if not mt5_ok and not args.synthetic:
            warn("MT5 not connected — signal server may fail to get live ticks")
            info("Continuing anyway (file-based fallback will be used)...")
        procs.append(start_signal_server(symbol))

    procs.append(start_dashboard())
    tg = start_telegram_bot()
    if tg:
        procs.append(tg)

    print_mt5_instructions(symbol)

    print(f"\n{BOLD}{GREEN}System running.{RESET} Press Ctrl+C to stop all processes.\n")

    try:
        # Wait — if any child dies unexpectedly, report it
        while True:
            import time
            time.sleep(3)
            for p in procs:
                if p.poll() is not None:
                    fail(f"Process {p.pid} exited with code {p.returncode}")
    except KeyboardInterrupt:
        print(f"\n{YELLOW}Shutting down...{RESET}")
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        print("All processes stopped.")


if __name__ == "__main__":
    main()
