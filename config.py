"""Central configuration for XAU/USD ATS.

Symbol-agnostic design: pass symbol explicitly via CLI (--symbol) or EA _Symbol.
SYMBOL constant kept as the default for backward-compatibility with existing modules.
"""
import os
from pathlib import Path

# Paths
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
MODEL_DIR = ROOT / "ai_models" / "checkpoints"
LOG_DIR = ROOT / "logs"

for d in (DATA_DIR, MODEL_DIR, LOG_DIR):
    d.mkdir(parents=True, exist_ok=True)

# MT5 Common Files path (shared by multi_runner, SignalFileWriter, LiveStateWriter)
MT5_FILES_PATH = Path(os.environ.get(
    "MT5_FILES_PATH",
    Path.home() / "AppData" / "Roaming" / "MetaQuotes"
    / "Terminal" / "Common" / "Files",
))

# Live shared-state file (read by Streamlit dashboard + Telegram bot)
LIVE_STATE_PATH = LOG_DIR / "live_state.json"

# ZeroMQ
ZMQ_SIGNAL_ADDR = "tcp://127.0.0.1:5555"
# ZMQ_TOPIC is now derived from symbol at runtime; kept for backward-compat
ZMQ_TOPIC = b"XAUUSD"

# Default symbol (backward-compat; prefer passing symbol explicitly)
SYMBOL = "XAUUSD"

# --------------------------------------------------------------------------
# Per-symbol trading configuration
#   spread_bps      : typical bid-ask spread in basis points
#   atr_mult_sl     : ATR multiplier for stop-loss
#   contract_size   : notional per 1 lot (oz for gold, units for forex)
#   eod_hour_gmt    : hard liquidation hour (GMT)
#   leverage        : broker leverage for this instrument
# --------------------------------------------------------------------------
SYMBOL_CONFIGS: dict[str, dict] = {
    "XAUUSD": {
        "spread_bps": 0.8,
        "atr_mult_sl": 1.5,
        "contract_size": 100.0,     # 1 lot = 100 troy oz
        "eod_hour_gmt": 22,
        "leverage": 2000,
    },
    "EURUSD": {
        "spread_bps": 0.3,
        "atr_mult_sl": 1.2,
        "contract_size": 100_000.0, # 1 lot = 100k units
        "eod_hour_gmt": 22,
        "leverage": 500,
    },
    "GBPUSD": {
        "spread_bps": 0.4,
        "atr_mult_sl": 1.3,
        "contract_size": 100_000.0,
        "eod_hour_gmt": 22,
        "leverage": 500,
    },
    "USDJPY": {
        "spread_bps": 0.3,
        "atr_mult_sl": 1.2,
        "contract_size": 100_000.0,
        "eod_hour_gmt": 22,
        "leverage": 500,
    },
    "BTCUSD": {
        "spread_bps": 5.0,
        "atr_mult_sl": 2.0,
        "contract_size": 1.0,
        "eod_hour_gmt": 22,
        "leverage": 100,
    },
    "NAS100": {
        "spread_bps": 1.5,
        "atr_mult_sl": 1.5,
        "contract_size": 1.0,
        "eod_hour_gmt": 21,
        "leverage": 200,
    },
}

_DEFAULT_SYMBOL_CONFIG: dict = {
    "spread_bps": 0.5,
    "atr_mult_sl": 1.5,
    "contract_size": 100.0,
    "eod_hour_gmt": 22,
    "leverage": 500,
}


def get_symbol_config(symbol: str) -> dict:
    """Return per-symbol config, falling back to defaults for unknown symbols."""
    return SYMBOL_CONFIGS.get(symbol.upper(), _DEFAULT_SYMBOL_CONFIG)


# --------------------------------------------------------------------------
# Global trading defaults (used when no per-symbol override is available)
# --------------------------------------------------------------------------
MAX_DRAWDOWN_PCT = 15.0
EOD_HOUR_GMT = 22
NO_NEW_TRADES_HOUR = 21
KELLY_FRACTION = 0.1
MAX_RISK_PER_TRADE = 0.02   # 2%
LEVERAGE = 2000
SPREAD_BPS = 0.8
ATR_MULT_SL = 1.5
VWAP_SLICE_THRESHOLD = 0.5  # lots

# RL
RL_LEARNING_RATE = 3e-4
RL_N_STEPS = 2048
RL_BATCH_SIZE = 64
RL_TOTAL_TIMESTEPS = 500_000
MAX_HOLD_BARS = 60
FEATURE_DIM = 24
ROLLING_WINDOW = 200

# Feature engineering
MINUTES_PER_YEAR = 252 * 1440   # trading minutes per year

# T-KAN
TKAN_SEQ_LEN = 50
TKAN_INPUT_DIM = 6
TKAN_CHEBY_ORDER = 4
TKAN_HIDDEN_DIM = 64
TKAN_NUM_CLASSES = 2  # range, trend

# --------------------------------------------------------------------------
# Auto-Retrainer
# --------------------------------------------------------------------------

# Drift detection: trigger retrain when rolling win-rate drops below this
DRIFT_WIN_RATE_THRESHOLD = 0.43     # < 43% wins in the rolling window

# Minimum closed trades before drift detection evaluates (avoids noise)
RETRAIN_MIN_TRADES = 30

# How often AutoRetrainer checks drift / weekly schedule (seconds)
RETRAIN_CHECK_INTERVAL = 3_600      # every 1 hour

# Weekly scheduled retrain: ISO weekday (0 = Monday … 6 = Sunday)
RETRAIN_WEEKLY_DAY = 0              # Monday

# Accept new model only if new_sharpe >= current_sharpe × this ratio
# 0.90 = new model allowed to be up to 10% worse than current (noise tolerance)
RETRAIN_MODEL_ACCEPT_RATIO = 0.90

# Bars used to evaluate old vs new model (out-of-sample comparison)
RETRAIN_EVAL_BARS = 5_000

# Bars fetched for each full retrain cycle (more data than bootstrap)
RETRAIN_BARS = 50_000

# PPO timesteps for scheduled/drift retrains (more thorough than bootstrap)
RETRAIN_TIMESTEPS = 300_000

