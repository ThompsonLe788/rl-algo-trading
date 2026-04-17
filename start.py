"""ATS System Startup Script.

Performs pre-flight checks then launches the multi-symbol runner +
Streamlit dashboard + optional Telegram bot as subprocesses.

Usage:
  python start.py                  # auto-detect open MT5 charts, start trading
  python start.py --dashboard-only # dashboard only, no trading
"""
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Force UTF-8 stdout so Unicode characters render on Windows CP1252 terminals
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    from dotenv import load_dotenv
    _env = ROOT / ".env"
    if _env.exists():
        load_dotenv(_env)
except ImportError:
    pass

from config import MODEL_DIR, MT5_FILES_PATH

GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
RESET  = "\033[0m"
BOLD   = "\033[1m"
CYAN   = "\033[96m"

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
            warn(f"MT5 not connected: {mt5.last_error()}")
            info("Start MetaTrader 5 and log in, then retry.")
            return False
        term = mt5.terminal_info()
        acc  = mt5.account_info()
        mt5.shutdown()
        ok(f"MetaTrader 5 connected ({term.name})")
        if acc:
            ok(f"Account #{acc.login}  balance=${acc.balance:,.2f}  equity=${acc.equity:,.2f}")
        return True
    except ImportError:
        warn("MetaTrader5 package not installed")
        return False


def check_open_charts() -> list[str]:
    try:
        symbols = []
        for f in MT5_FILES_PATH.glob("ats_chart_*.txt"):
            for enc in ("utf-16", "utf-8", "latin-1"):
                try:
                    if f.read_text(encoding=enc).strip() == "1":
                        symbols.append(f.stem.replace("ats_chart_", "").upper())
                        break
                except Exception:
                    pass
        return symbols
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Launch subprocesses
# ---------------------------------------------------------------------------

def start_runner() -> subprocess.Popen:
    print(f"\n{BOLD}Starting runner...{RESET}")
    proc = subprocess.Popen([sys.executable, "main.py", "run"], cwd=ROOT)
    ok(f"Runner PID {proc.pid} — auto-detects all open MT5 charts")
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


def start_telegram_bot() -> "subprocess.Popen | None":
    if not os.environ.get("TELEGRAM_TOKEN"):
        warn("TELEGRAM_TOKEN not set — Telegram bot disabled")
        return None
    proc = subprocess.Popen(
        [sys.executable, "dashboard/telegram_bot.py"],
        cwd=ROOT,
    )
    ok(f"Telegram bot PID {proc.pid}")
    return proc


# ---------------------------------------------------------------------------
# MT5 EA setup instructions
# ---------------------------------------------------------------------------

def print_mt5_instructions():
    print(f"""
{BOLD}MT5 Setup (manual steps){RESET}
  For EACH symbol you want to trade:
  1. Open the chart in MetaTrader 5
  2. Drag {BOLD}XauDayTrader{RESET} EA  ->  MQL5\\Experts\\
     - ZmqAddress : tcp://127.0.0.1:5555
     - MagicNumber: 20250411
  3. Drag {BOLD}ATS_Panel{RESET} indicator  ->  MQL5\\Indicators\\
     - Panel shows live state top-left

  The runner auto-detects every open chart. Supported symbols:
  XAUUSD, EURUSD, GBPUSD, USDJPY, BTCUSD, NAS100, and more.
""")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="ATS System Startup")
    parser.add_argument(
        "--dashboard-only", action="store_true",
        help="Start Streamlit dashboard only (no trading)",
    )
    args = parser.parse_args()

    print(f"\n{BOLD}{CYAN}{'='*55}")
    print(f"  ATS Multi-Symbol Trading System")
    print(f"{'='*55}{RESET}\n")

    if not check_deps():
        sys.exit(1)

    mt5_ok = check_mt5()

    if not args.dashboard_only:
        open_charts = check_open_charts()
        if open_charts:
            ok(f"Open charts detected: {', '.join(open_charts)}")
            for sym in open_charts:
                path = MODEL_DIR / f"ppo_{sym.lower()}.zip"
                if path.exists():
                    ok(f"  Model: {path.name}")
                else:
                    warn(f"  No model for {sym} — will auto-train on first run (~3 min)")
        else:
            warn("No open MT5 charts detected yet")
            info("Open a chart and attach ATS_Panel — it will auto-register")

    if not mt5_ok and not args.dashboard_only:
        warn("MT5 not connected — runner will retry every 5s until connected")

    print()
    procs = []

    if not args.dashboard_only:
        procs.append(start_runner())

    procs.append(start_dashboard())

    tg = start_telegram_bot()
    if tg:
        procs.append(tg)

    print_mt5_instructions()

    print(f"\n{BOLD}{GREEN}System running.{RESET}")
    print(f"  Dashboard : http://localhost:8501")
    print(f"  Status    : python main.py status")
    print(f"  Ctrl+C    : stop all\n")

    try:
        while True:
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
