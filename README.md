# XAU/USD Reinforcement Learning Algorithmic Trading System

A production-grade automated trading system (ATS) for **XAU/USD (Gold)** using Reinforcement Learning, built on Python + MetaTrader 5.

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![MT5](https://img.shields.io/badge/MetaTrader-5-orange)
![License](https://img.shields.io/badge/license-MIT-green)
![Tests](https://img.shields.io/badge/tests-38%20passing-brightgreen)

---

## Overview

The system trains a **PPO reinforcement learning agent** on M1 bar data, then executes intraday trades in real-time using tick data. It supports multiple symbols simultaneously with automatic worker management and self-retraining.

```
MT5 Terminal (tick data)
    │
    ▼
Python: LiveTickStream → 24-dim Feature Vector
    │    OU z-score, ATR, VWAP, Momentum, LOB proxy, Time encoding
    ▼
PPO Agent → Decision: Long / Short / Hold / Close
    │
    ▼
Risk Manager: Fractional Kelly (1/10), Kill Switch (15% MDD), ATR trailing stop
    │
    ▼
ZeroMQ PUB → XauDayTrader EA → OrderSend()
    │
    ├── ATS_Panel Indicator (top-left panel: 7 sections, live state)
    ├── Streamlit Dashboard (localhost:8501)
    └── Telegram Bot (real-time alerts)
```

---

## Key Features

| Feature | Detail |
|---|---|
| **Strategy** | Regime-adaptive RL: mean-reversion (RANGE) + trend-following (TREND) |
| **Timeframe** | Training: M1 bars (50,000 ≈ 35 days) · Live: tick execution |
| **Models** | PPO (default), SAC (sample-efficient), T-KAN (regime classifier) |
| **Position sizing** | Fractional Kelly Criterion (1/10 Kelly) |
| **Risk controls** | Kill switch (MDD 15%), EOD close (22h GMT), ATR trailing stop |
| **Multi-symbol** | XAUUSD, EURUSD, GBPUSD, USDJPY, BTCUSD, NAS100 — auto-detection |
| **Auto-retrain** | Weekly (Monday) + drift detection (win_rate < 43%), live model hot-swap |
| **MT5 Panel** | Top-left indicator: 7 sections — MARKET, POSITION, SIGNALS, RISK, AI MODEL, SYSTEM |
| **Latency** | ZMQ PUB/SUB < 1ms signal delivery |
| **Walk-forward** | Rolling backtest with TCA (slippage, market impact) |

---

## Architecture

```
d:/xau_ats/
├── ai_models/
│   ├── features.py          ← 24 features: OU z-score, ATR, VWAP, LOB, momentum
│   ├── rl_agent.py          ← PPO/SAC train & inference (stable-baselines3)
│   ├── regime_tkan.py       ← T-KAN regime classifier (RANGE/TREND)
│   ├── trading_env.py       ← Gym environment with Sharpe-adjusted reward
│   └── checkpoints/         ← Trained model weights (ppo_SYMBOL.zip)
├── backtest/
│   ├── walkforward.py       ← Walk-forward engine with TimeSeriesSplit
│   └── tca.py               ← Transaction cost analysis
├── dashboard/
│   ├── app.py               ← Streamlit dashboard (localhost:8501)
│   ├── state_reader.py      ← live_state.json parser
│   └── telegram_bot.py      ← Telegram alerts
├── data/
│   └── pipeline.py          ← MT5 fetch, Parquet cache, LiveTickStream, synthetic GBM+OU
├── docs/
│   ├── user_guide.md        ← Installation & operation guide (Vietnamese)
│   ├── service_handbook.md  ← Daily ops & incident response (Vietnamese)
│   └── technical_guide.md   ← Full technical documentation (Vietnamese)
├── mt5_bridge/
│   ├── ATS_Panel.mq5        ← MT5 Indicator (top-left panel, 7 sections)
│   ├── XauDayTrader.mq5     ← MT5 Expert Advisor (ZMQ → OrderSend)
│   ├── ATS_StrategyView.mq5 ← MT5 Indicator (chart overlays: VWAP, signals)
│   ├── multi_runner.py      ← Multi-symbol orchestrator, shared ZMQ socket
│   ├── signal_server.py     ← ZMQ PUB server + LiveStateWriter singleton
│   └── auto_retrainer.py    ← AutoRetrainer: weekly + drift-triggered hot-swap
├── risk/
│   ├── kelly.py             ← Fractional Kelly with Bayesian win rate, VWAP slicer
│   ├── kill_switch.py       ← MDD/daily loss/EOD liquidation
│   └── news_filter.py       ← High-impact news blackout window
├── config.py                ← All parameters (risk, timing, ZMQ, training, symbols)
├── main.py                  ← CLI entry point
├── start.py                 ← Startup script (pre-flight checks + launch)
└── tests/test_ats.py        ← 38 test cases
```

---

## Requirements

- Windows 10/11 (64-bit)
- Python 3.11+
- MetaTrader 5 (any broker with XAU/USD)
- ZeroMQ DLL for MT5 ([mql-zmq](https://github.com/dingmaotu/mql-zmq))

---

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Copy environment template
copy .env.example .env
# Edit .env: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID (optional)

# 3. Start MetaTrader 5 and log in

# 4. Start the trading backend
python main.py multi-live

# 5. Start the dashboard (new terminal)
streamlit run dashboard/app.py
# → Open http://localhost:8501

# 6. In MT5 for each symbol chart:
#    a. Drag XauDayTrader EA   → MQL5\Experts\    (executes orders via ZMQ)
#    b. Drag ATS_Panel         → MQL5\Indicators\ (top-left live panel)
#    → System auto-detects, trains model (~3-5 min first time), goes live
```

---

## MT5 File Roles

| File | Type | Destination | Role |
|---|---|---|---|
| `ATS_Panel.mq5` | **Indicator** | `MQL5\Indicators\` | Live state panel + symbol detection |
| `XauDayTrader.mq5` | **Expert Advisor** | `MQL5\Experts\` | Receives ZMQ signals, places orders |
| `ATS_StrategyView.mq5` | Indicator | `MQL5\Indicators\` | Chart overlays (VWAP, signal arrows) |

---

## ATS_Panel — Live State Display

The panel appears **top-left** on any chart with `ATS_Panel` attached. It reads `ats_live_state.json` (Python → MT5 Common Files) and updates every heartbeat (~10s).

```
┌─────────────────────────────────┐
│ ATS PANEL — XAUUSD              │
├─── MARKET ──────────────────────┤
│  Chart TF: M1  │ ATS TF: M1(RL) │
│  Session: London│ Spread: 0.8bp  │
├─── POSITION ────────────────────┤
│  LONG          │ RANGE           │
│  Entry: 3200.50  Unrealized: +12 │
│  SymDD: 0.5%   │ AcctDD: 0.5%   │
├─── SIGNALS (last 5) ────────────┤
│  Dir│Price  │Win%│R/R│Lot│Time  │
│  BUY│3200.50│57% │1.8│0.1│08:32 │
├─── RISK ────────────────────────┤
│  Kelly f: 1.67%                  │
│  Equity: 10,120 │Balance: 10,000 │
├─── AI MODEL ────────────────────┤
│  ppo_xauusd [2026-04-15 23:32]  │
│  Status: Running                 │
│  WinRate: 55.0% │ Sharpe: 1.23  │
│  Trades: 34                      │
├─── SYSTEM ──────────────────────┤
│  Kill: ---   HB: 3s ago          │
│  Updated: 10:38:02               │
└─────────────────────────────────┘
```

---

## Configuration

All parameters are centralized in `config.py`:

```python
# Risk
MAX_DRAWDOWN_PCT    = 15.0    # Kill switch threshold
MAX_RISK_PER_TRADE  = 0.02    # Max 2% equity per trade
KELLY_FRACTION      = 0.1     # 1/10 Kelly (conservative)
EOD_HOUR_GMT        = 22      # Close all positions at 22:00 GMT

# Auto-Retrain
DRIFT_WIN_RATE_THRESHOLD = 0.43   # Retrain when win rate < 43%
RETRAIN_WEEKLY_DAY       = 0      # Monday (ISO weekday)
RETRAIN_MODEL_ACCEPT_RATIO = 0.90 # Accept new model if Sharpe >= 90% of current
RETRAIN_BARS             = 50_000 # M1 bars for retrain
RETRAIN_TIMESTEPS        = 300_000

# Per-symbol configs in SYMBOL_CONFIGS dict (spread, leverage, lot limits, digits)
```

---

## How It Works

### 1. Feature Engineering (24 features)
- **OU z-score**: Mean-reversion signal using Ornstein-Uhlenbeck process
- **OU MLE params**: θ (speed), μ deviation, half-life via rolling OLS regression
- **ATR**: Volatility for dynamic stop-loss sizing
- **VWAP deviation**: Distance from daily VWAP, normalized by ATR
- **Momentum**: Log-returns at 5/15/60-bar horizons
- **LOB imbalance proxy**: Volume × sign(Δprice) / rolling average
- **Time encoding**: Circular sin/cos of GMT hour (session awareness)
- **Realized volatility**: Annualized 50-bar rolling vol

### 2. Regime Detection (T-KAN)
Classifies market as RANGE (mean-reverting) or TREND. Architecture:
```
Input (50 bars × 6 features) → ChebyshevBasis(order=4) → GRU → Softmax
```

### 3. RL Agent (PPO)
- **Action space**: {Hold, Long, Short, Close} (discrete, 4 actions)
- **Reward**: Sharpe-adjusted P&L minus drawdown penalty
- **Max hold**: 60 bars (60 minutes intraday)

### 4. Risk Management
- Kelly fraction computed per-symbol from rolling win rate (Bayesian Beta prior)
- Trailing stop: `trail = entry ± 1.5 × ATR`, updates each tick
- Kill switch: triggers at 15% drawdown, 5% daily loss, or 22:00 GMT

### 5. Auto-Retraining
- **Weekly**: every Monday, once per ISO week
- **Drift**: when rolling win-rate < 43% (min 30 trades, 24h cooldown)
- **Evaluation**: 3 time-window walk-forward × 2 seeds → mean Sharpe gate
- **Hot-swap**: new model replaces live model without trading interruption
- **Rollback**: old model auto-archived as `ppo_SYMBOL_backup_YYYYMMDD_HHMM.zip`

---

## Backtest

```bash
# Walk-forward backtest (3 months, XAUUSD)
python main.py backtest --symbol XAUUSD --months 3
```

Outputs per fold: Sharpe ratio, max drawdown, win rate, profit factor, TCA breakdown.

---

## Tests

```bash
python -m pytest tests/ -v
```

38 test cases covering feature engineering, RL environment, risk management, data pipeline, and signal server.

---

## Documentation

Full documentation in Vietnamese in `docs/`:

| Document | Description |
|---|---|
| [`user_guide.md`](docs/user_guide.md) | Installation, configuration, daily operation, FAQ |
| [`service_handbook.md`](docs/service_handbook.md) | Daily SOP, incident response, maintenance, retrain schedule |
| [`technical_guide.md`](docs/technical_guide.md) | Algorithms, model selection, pros/cons |

---

## Disclaimer

**This software is for educational and research purposes only.**

- Past performance does not guarantee future results
- Algorithmic trading carries significant financial risk
- Always test on a demo account before using real capital
- The kill switch (15% MDD) is a safety mechanism, not a guarantee against loss
- Use at your own risk

---

## License

MIT License — see [LICENSE](LICENSE) for details.
