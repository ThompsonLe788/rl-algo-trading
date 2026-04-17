"""Central configuration — Multi-Symbol ATS.

Symbol-agnostic design: pass symbol explicitly via CLI (--symbol) or EA _Symbol.
All per-symbol parameters (leverage, contract size, spread, digits, max lot) are
defined in SYMBOL_CONFIGS and looked up via get_symbol_config(symbol).
SYMBOL / LEVERAGE / SPREAD_BPS constants are kept only as generic fallback defaults.
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
if not MT5_FILES_PATH.exists():
    import warnings
    warnings.warn(
        f"MT5_FILES_PATH does not exist: {MT5_FILES_PATH}\n"
        "Set the MT5_FILES_PATH environment variable to the correct path.",
        stacklevel=2,
    )

# Live shared-state file (read by Streamlit dashboard + Telegram bot)
LIVE_STATE_PATH = LOG_DIR / "live_state.json"

# ZeroMQ
ZMQ_SIGNAL_ADDR = "tcp://127.0.0.1:5555"
# ZMQ_TOPIC: live signal_server derives topic from symbol at runtime.
# Kept here for test_zmq_bridge.py only — do NOT use in production code.
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
        "price_digits": 2,          # MT5 SYMBOL_DIGITS equivalent for rounding
        "lot_precision": 2,         # lot step = 0.01 (standard MT5)
        "max_lot": 50.0,
    },
    "EURUSD": {
        "spread_bps": 0.3,
        "atr_mult_sl": 1.2,
        "contract_size": 100_000.0, # 1 lot = 100k units
        "eod_hour_gmt": 22,
        "leverage": 500,
        "price_digits": 5,          # 1.35640 — needs 5 decimals
        "lot_precision": 2,
        "max_lot": 20.0,
    },
    "GBPUSD": {
        "spread_bps": 0.4,
        "atr_mult_sl": 1.3,
        "contract_size": 100_000.0,
        "eod_hour_gmt": 22,
        "leverage": 500,
        "price_digits": 5,
        "lot_precision": 2,
        "max_lot": 20.0,
    },
    "USDJPY": {
        "spread_bps": 0.3,
        "atr_mult_sl": 1.2,
        "contract_size": 100_000.0,
        "eod_hour_gmt": 22,
        "leverage": 500,
        "price_digits": 3,          # 149.820 — 3 decimals
        "lot_precision": 2,
        "max_lot": 20.0,
    },
    "BTCUSD": {
        "spread_bps": 5.0,
        "atr_mult_sl": 2.0,
        "contract_size": 1.0,
        "eod_hour_gmt": 22,
        "leverage": 100,
        "price_digits": 2,
        "lot_precision": 2,
        "max_lot": 5.0,
    },
    "NAS100": {
        "spread_bps": 1.5,
        "atr_mult_sl": 1.5,
        "contract_size": 1.0,
        "eod_hour_gmt": 21,
        "leverage": 200,
        "price_digits": 2,
        "lot_precision": 2,
        "max_lot": 50.0,
    },
    "AUDUSD": {
        "spread_bps": 0.3,
        "atr_mult_sl": 1.2,
        "contract_size": 100_000.0, # 1 lot = 100k AUD
        "eod_hour_gmt": 22,
        "leverage": 500,
        "price_digits": 5,
        "lot_precision": 2,
        "max_lot": 5.0,             # conservative cap for 100K FTMO
    },
    "NZDUSD": {
        "spread_bps": 0.4,
        "atr_mult_sl": 1.2,
        "contract_size": 100_000.0,
        "eod_hour_gmt": 22,
        "leverage": 500,
        "price_digits": 5,
        "lot_precision": 2,
        "max_lot": 5.0,
    },
    "USDCAD": {
        "spread_bps": 0.4,
        "atr_mult_sl": 1.2,
        "contract_size": 100_000.0,
        "eod_hour_gmt": 22,
        "leverage": 500,
        "price_digits": 5,
        "lot_precision": 2,
        "max_lot": 5.0,
    },
    "USDCHF": {
        "spread_bps": 0.4,
        "atr_mult_sl": 1.2,
        "contract_size": 100_000.0,
        "eod_hour_gmt": 22,
        "leverage": 500,
        "price_digits": 5,
        "lot_precision": 2,
        "max_lot": 5.0,
    },
}

_DEFAULT_SYMBOL_CONFIG: dict = {
    "spread_bps": 0.5,
    "atr_mult_sl": 1.5,
    "contract_size": 100_000.0,  # safe default for unknown forex pairs
    "eod_hour_gmt": 22,
    "leverage": 500,
    "price_digits": 5,
    "lot_precision": 2,          # default lot step = 0.01
    "max_lot": 3.0,              # conservative cap for unknown symbols
}


def get_symbol_config(symbol: str) -> dict:
    """Return per-symbol config, falling back to defaults for unknown symbols.

    Adds a derived 'min_lot' key (= 10^-lot_precision) so callers never need
    to hardcode 0.01 or similar fallbacks.
    """
    cfg = dict(SYMBOL_CONFIGS.get(symbol.upper(), _DEFAULT_SYMBOL_CONFIG))
    if "min_lot" not in cfg:
        cfg["min_lot"] = 10 ** (-cfg["lot_precision"])
    return cfg


# --------------------------------------------------------------------------
# Global trading defaults (used when no per-symbol override is available)
# --------------------------------------------------------------------------
MAX_DRAWDOWN_PCT = 10.0
DAILY_LOSS_LIMIT_PCT = 5.0  # standard mode daily loss limit (% of equity)
PROFIT_TARGET_PCT = 10.0    # FTMO-style profit target (% of initial balance)
EOD_HOUR_GMT = 22
NO_NEW_TRADES_HOUR = 21
KELLY_FRACTION = 0.1
MAX_RISK_PER_TRADE = 0.02   # 2%
LEVERAGE = 500              # generic fallback; per-symbol in SYMBOL_CONFIGS
SPREAD_BPS = 0.5            # generic fallback; per-symbol in SYMBOL_CONFIGS
ATR_MULT_SL = 1.5
VWAP_SLICE_THRESHOLD = 0.5  # lots
SCAN_INTERVAL = 5           # seconds between open-chart scans
AUTO_TRAIN_TIMESTEPS = 200_000  # PPO steps for first-run bootstrap training

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
# Kelly / Bayesian risk constants
# --------------------------------------------------------------------------
KELLY_PRIOR_ALPHA     = 2.0    # Beta prior α for Bayesian win-rate
KELLY_PRIOR_BETA      = 2.0    # Beta prior β
PNL_SCRATCH_THRESHOLD = 0.01   # |pnl| below this → scratch (neither win nor loss)
DD_TAPER_START_PCT    = 5.0    # drawdown % where Kelly taper begins → 0 at MAX_DRAWDOWN_PCT

# Volatility regime z-score thresholds (hysteresis state machine)
VOL_REGIME_SPIKE_ENTER    = 2.5   # z > this  → enter spike state  (scalar 0.50×)
VOL_REGIME_SPIKE_EXIT     = 1.5   # z < this  → drop to elevated
VOL_REGIME_ELEVATED_ENTER = 1.5   # z > this  → enter elevated     (scalar 0.75×)
VOL_REGIME_ELEVATED_EXIT  = 0.5   # z < this  → back to normal

# --------------------------------------------------------------------------
# SL/TP optimizer constants
# --------------------------------------------------------------------------
TP_MULT_DEFAULT  = 2.0   # TP = SL × this when no bucket data available
TP_SAFETY_MARGIN = 1.1   # TP must exceed Kelly break-even R by this factor
# Win-rate → SL multiplier lookup table (checked top-to-bottom, first match wins)
# Format: (win_rate_threshold, sl_multiplier)
SL_ADJ_TABLE: list[tuple[float, float]] = [
    (0.60, 0.80),   # high accuracy  → tighter stop
    (0.50, 1.00),   # neutral
    (0.45, 1.15),
    (0.40, 1.25),
    (0.00, 1.50),   # low accuracy   → widen stop (catch-all)
]

# --------------------------------------------------------------------------
# News filter
# --------------------------------------------------------------------------
NEWS_BLACKOUT_BEFORE_MINS = 15   # block trading N minutes before high-impact news
NEWS_BLACKOUT_AFTER_MINS  = 10   # block trading N minutes after

# --------------------------------------------------------------------------
# ZMQ / networking
# --------------------------------------------------------------------------
HEARTBEAT_INTERVAL = 5.0    # seconds between Python→EA heartbeat pings
HEARTBEAT_ENABLED  = False  # set True to send ZMQ heartbeat frames to EA
SPREAD_SPIKE_MULT  = 3.0    # block entry when spread > this × rolling average

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
# 0.95 = new model must reach ≥95% of current Sharpe.
# Tightened from 0.90: 3-window × 4-seed eval (12 runs) reduces noise enough
# that 5% tolerance is achievable without rejecting genuinely better models.
RETRAIN_MODEL_ACCEPT_RATIO = 0.95

# Bars used to evaluate old vs new model (out-of-sample comparison).
# Increased to 10k so each of 3 windows has ~3333 bars (~2.3 days of M1 data).
RETRAIN_EVAL_BARS = 10_000

# Bars fetched for each full retrain cycle (more data than bootstrap)
RETRAIN_BARS = 50_000

# PPO timesteps for scheduled/drift retrains (more thorough than bootstrap)
RETRAIN_TIMESTEPS = 300_000

