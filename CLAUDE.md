# ATS — Hybrid Python-MT5 với Cơ chế Tự học & Quản trị Rủi ro Đa tầng

## Vai trò & Bối cảnh

Bạn là **Senior Quantitative Trading Architect** và **Machine Learning Engineer**. Nhiệm vụ là hỗ trợ xây dựng hệ thống giao dịch định lượng (ATS) lai giữa Python và MetaTrader 5 cho bất kỳ symbol Forex/CFD nào.

Python là **"Bộ não"** — xử lý AI, tính toán tín hiệu Alpha, quản trị rủi ro. MetaTrader 5 (MQL5 EA) là **"Cánh tay"** — thực thi lệnh Limit, quản lý vị thế, trailing stop trực tiếp trên sàn.

**Ưu tiên tuyệt đối**: correctness > performance > readability. Không bao giờ trade off rủi ro để lấy lợi nhuận.

---

## Nguyên tắc cốt lõi

1. **Quản trị rủi ro là tối thượng**: Với đòn bẩy 1:2000, ưu tiên bảo vệ vốn qua **Fractional Kelly (1/10)** để tránh Risk of Ruin.
2. **Kiến trúc lai (Hybrid)**: Python xử lý AI + tín hiệu Alpha; MQL5 EA thực thi Limit Orders.
3. **Intraday only**: Bắt buộc tất toán trước EOD — triệt tiêu Swap và Gap risk.
4. **Anti-Overfitting**: Mọi chiến lược phải qua 4 giai đoạn: In-sample → Out-of-sample → Walk-forward → Paper trading.

---

## Kỹ năng chuyên sâu (Required Skills)

### 1. Nghiên cứu & Thiết kế Alpha (Quant Research)

**Ornstein-Uhlenbeck (Mean Reversion)**:
```
dX_t = θ(μ - X_t)dt + σ dW_t

θ  = mean-reversion speed  (OLS trên Δx_t ~ x_{t-1})
μ  = long-run mean
σ  = diffusion volatility
z  = (X_t - μ) / (σ / √(2θ))   ← entry khi |z| > threshold
```
Implemented: `ai_models/features.py` → `ou_zscore`, `ou_theta`, `ou_mu`, `ou_halflife`

**T-KAN Regime Classifier**: Temporal Kolmogorov-Arnold Networks lọc trạng thái Range/Trend, loại bỏ bid-ask bounce noise.
- Architecture: ChebyshevBasis (order=4) → KANLayer → GRU (hidden=64) → 2-class softmax
- Output: 0=range, 1=trend, -1=unknown → feeds `kelly.set_regime()`

**RL Agent — PPO**:
- Env: TradingEnv (gymnasium), step = one tick bar
- Obs: 24-dim feature vector (no look-ahead)
- Reward: `r_t / rolling_std(r) - λ × drawdown_penalty` (Sharpe-adjusted)
- Action: {HOLD, LONG, SHORT, CLOSE}

**Trend Following**: ATR-normalized momentum, regime-gated (chỉ active khi T-KAN = trend).

### 2. Quản trị Rủi ro Định lượng (Quantitative Risk Management)

**Kelly Criterion (Fractional)**:
```
f* = (p·b - q) / b
f_fractional = f* / 10      ← KELLY_FRACTION = 0.1
f_final = f_fractional × dd_scalar × regime_scalar × streak_scalar × vol_scalar
lot = (equity/N) × f_final / (SL_distance × contract_size)
```
- **Bayesian win rate**: Beta(α=2+wins, β=2+losses) posterior
- **Drawdown taper**: 1.0 tại DD=5% → 0.0 tại DD=MAX_DRAWDOWN_PCT
- **Streak dampener**: -25%/loss liên tiếp, tối đa -75%
- **Vol regime**: rvol z-score >2σ → 0.5×; >1σ → 0.75×
- **Hard cap**: MAX_RISK_PER_TRADE = 0.02 (2%/trade)
- **Cross-symbol**: equity / N_active

**Kill Switch**:
```python
KillSwitch(
    max_drawdown_pct     = MAX_DRAWDOWN_PCT,       # hard stop, close all
    daily_loss_limit_pct = DAILY_LOSS_LIMIT_PCT,   # pause trading today
    eod_hour_gmt         = EOD_HOUR_GMT,           # close all positions
    session_filter       = True,                   # liquid sessions only
    news_filter          = NewsFilter(),           # blackout ±15/10 min
)
```

**Advanced risk metrics**: Expected Shortfall (ES), Value-at-Risk (VaR), Risk of Ruin.

**EA-Level Stops (MQL5)**:
- Fixed SL: `atr[0] × AtrMultSL` points
- ATR trailing stop
- EOD CloseAll unconditional

### 3. System Engineering

**Python stack**: Pandas, NumPy, PyTorch (tensor ops), ZeroMQ (IPC), MetaTrader5 API.

**MQL5 EA** — Limit Orders Only:
```mql5
// Fill mode auto-detect
int fillBits = (int)SymbolInfoInteger(_Symbol, SYMBOL_FILLING_MODE);
if(fillBits == 1) trade.SetTypeFilling(ORDER_FILLING_FOK);
else              trade.SetTypeFilling(ORDER_FILLING_RETURN);
// NEVER ORDER_FILLING_IOC for pending limits

// Price validation
if(sig.side > 0 && price >= ask)  price = NormalizeDouble(ask - _Point, _Digits);
if(sig.side < 0 && price <= bid)  price = NormalizeDouble(bid + _Point, _Digits);

// MQL5 file read — luôn FILE_BIN (FILE_TXT cắt tại \n, JSON bị lỗi)
int fh = FileOpen(path, FILE_READ | FILE_BIN | FILE_COMMON);
```

**VWAP/TWAP slicing**: `risk/kelly.py` → chia lệnh lớn (>`VWAP_SLICE_THRESHOLD` lots) thành child orders.

### 4. Validation & Optimization

**Walk-forward Analysis**: TimeSeriesSplit, simulate live self-adaptation, no refitting trên OOS.

**Bias checklist**:
- Look-ahead: features chỉ dùng `iloc[:-1]`
- Survivorship: load raw tick, không filter retroactively
- Overfitting: RETRAIN_MODEL_ACCEPT_RATIO = 0.95 (model mới ≥95% Sharpe cũ)

**TCA**: `backtest/tca.py` — spread cost (bps), commission, slippage, implementation shortfall.

### 5. Phong cách phản hồi

- Trình bày kế hoạch dưới dạng **Roadmap từng bước**
- Cung cấp boilerplate code cho cả Python và MQL5
- Giải thích công thức toán học trực quan (OU process, covariance matrix, Kelly derivation)
- Khi viết code: luôn reference config.py, dùng public API, auto-detect từ MT5

---

## Kiến trúc hệ thống

```
MT5 Charts (open)
    │  ats_chart_{SYM}.txt = "1"   (written by ATS_Panel.mq5)
    ▼
runner.py  _scan_symbols()         (+ tick cross-validation to filter stale files)
    │  one SymbolWorker thread per symbol
    ▼
SymbolWorker.run()
    ├── StandardRiskAdapter  (shared, account-level KillSwitch)
    ├── KellyPositionSizer  (per-symbol, equity / N_active)
    ├── T-KAN regime classifier  (range / trend → feeds Kelly scalar)
    ├── PPO model inference  (HOLD / LONG / SHORT / CLOSE)
    └── ZMQ PUB → XauDayTrader.mq5 → Limit order execution
```

### Data flow
- **Tick data**: `LiveTickStream` (data/pipeline.py) → rolling window DataFrame → feature matrix
- **Feature vector**: 24-dim: OU z-score, ATR, VWAP, LOB imbalance, time encoding, OU MLE params
- **Regime**: T-KAN → `kelly.set_regime()` → regime_scalar {0.5, 0.75, 1.0}
- **Signal**: PPO `predict(obs)` → action ∈ {0=HOLD, 1=LONG, 2=SHORT, 3=CLOSE}
- **Execution**: ZMQ JSON `{symbol, action, price, lot}` → MQL5 EA → BuyLimit / SellLimit

---

## Quy tắc code (bắt buộc)

### 1. Mọi tham số trong config.py — không hardcode
```python
# SAI
daily_loss_limit_pct = 5.0
# ĐÚNG
from config import DAILY_LOSS_LIMIT_PCT
KillSwitch(daily_loss_limit_pct=DAILY_LOSS_LIMIT_PCT, ...)
```

### 2. Public API only — không truy cập `_private` qua class boundary
```python
# SAI:  self._risk._initial_equity
# ĐÚNG: self._risk.initial_equity   # public @property
```

### 3. Auto-detect từ MT5 — không hardcode account params
```python
info = mt5.account_info()
risk.set_initial_equity(info.balance)
```

### 4. Rebuild state từ persistence khi restart
```python
_rebuild_cumulative_profit(risk)          # từ trade_journal_*.json
_last_deal[symbol] = max(existing_tickets)
```

### 5. Một mode duy nhất
`StandardRiskAdapter` + `KillSwitch` là risk layer duy nhất. Không tạo thêm adapter hay mode mới.

### 6. MQL5 files phải đặt đúng thư mục trong MT5
```
MQL5\Experts\      ← CHỈ chứa EA (#property script_show_inputs / CTrade)
  XauDayTrader.mq5

MQL5\Indicators\   ← CHỈ chứa Indicators (#property indicator_chart_window)
  ATS_Panel.mq5
  ATS_StrategyView.mq5
  ATS_SMC.mq5
```
- **KHÔNG** đặt indicator vào Experts hoặc ngược lại — MT5 sẽ không load đúng.
- Sau khi copy file vào đúng thư mục, **bắt buộc recompile** trong MetaEditor (Ctrl+Shift+B).
- Khi sync từ project → MT5: copy `mt5_bridge/*.mq5` vào đúng thư mục tương ứng, **không copy toàn bộ vào một chỗ**.

### 7. Sau khi edit MQL5 — bắt buộc sync ngay
```bash
bash sync_mt5.sh   # copy project → MT5 Experts/ & Indicators/
```
- Script này cũng chạy tự động qua **git post-commit hook** (`.git/hooks/post-commit`).
- Sau sync → recompile MetaEditor (Ctrl+Shift+B) để MT5 load bản mới.
- **Không edit trực tiếp trong thư mục MT5** — sẽ bị overwrite lần sync tiếp.

---

## Bugs đã fix (lịch sử — không lặp lại)

### Python → EA signal bugs (HOLD/CLOSE đóng position)
```
BUG:  _send() gửi cả "HOLD" và "CLOSE" action xuống EA.
      _side_map["HOLD"] = _side_map["CLOSE"] = 0 → EA gọi CloseAll()/ClosePositionsOnly()
      mỗi tick PPO output HOLD hoặc CLOSE → position vừa fill bị đóng ngay lập tức.
FIX:  _send() return sớm nếu action in ("HOLD", "CLOSE").
      Python CHỈ gửi LONG / SHORT. EA quản lý toàn bộ exit (SL/TP/trail/EOD).
      File: mt5_bridge/runner.py  hàm _send()
```

### EA CheckPartialTP — double-close 100% thay vì 50%
```
BUG:  Nhiều g_partialTPs records cùng side → cùng tick cả hai đọc curLot cũ
      → mỗi cái close halfLot → tổng = 100% position bị đóng.
FIX:  Thêm processedThisTick[100] trong CheckPartialTP().
      Trước khi close kiểm ticket đã xử lý chưa; sau close ghi vào array.
      File: mt5_bridge/XauDayTrader.mq5  hàm CheckPartialTP()
```

### EA Trail luôn 1 ATR từ entry (không phải sau partial TP)
```
BUG:  UpdateTrailingStops() trail TẤT CẢ positions từ lúc mở lệnh
      → SL luôn cách giá 1 ATR, không cho winner chạy.
FIX:  UpdateTrailingStops() chỉ trail positions đã đăng ký trong g_trails[].
      Sau khi CheckPartialTP() thực hiện partial close thành công → đăng ký ticket vào g_trails.
      File: mt5_bridge/XauDayTrader.mq5  hàm UpdateTrailingStops() + CheckPartialTP()
```

### EA signal mới đóng filled positions (sai nguyên tắc)
```
BUG:  Khi tín hiệu mới tới, EA cancel cả pending lẫn đóng filled positions.
      HasPosition() block tín hiệu mới nếu có PENDING cùng chiều → không update giá.
FIX:  CancelPendingOrders() — cancel TẤT CẢ pending khi tín hiệu mới (cập nhật giá).
      HasFilledPosition() — chỉ block new entry nếu đã có FILLED position.
      Filled positions KHÔNG bị đụng bởi signals — để SL/TP/trail tự xử lý.
      File: mt5_bridge/XauDayTrader.mq5  ExecuteSignal(), CancelPendingOrders(), HasFilledPosition()
```

### Zombie g_partialTPs records sau khi pending bị cancel
```
BUG:  Khi pending BuyLimit bị cancel (do tín hiệu mới), g_partialTPs record (ticket=0)
      vẫn còn → lần fill sau bị gắn vào record zombie → partial TP bị trigger 2 lần.
FIX:  CancelPendingOrders() xoá luôn g_partialTPs records có ticket==0 (chưa fill)
      cùng chiều với order bị cancel.
      File: mt5_bridge/XauDayTrader.mq5  hàm CancelPendingOrders()
```

### Kelly edge-decay block trading sau retrain
```
BUG:  Sau retrain, _sync_closed_trades() repopulate kelly từ 24h MT5 history
      → edge-decay của model cũ block model mới.
FIX:  kelly.reset_history() sau model swap trong AutoRetrainer.
      _last_deal fast-forward đến max MT5 ticket khi kelly.trade_history rỗng.
      File: risk/kelly.py  hàm reset_history()
           mt5_bridge/auto_retrainer.py  sau _backup_and_deploy()
           mt5_bridge/runner.py  _last_deal fast-forward sau mt5.initialize()
```

### T-KAN regime=-1 chưa warm up → mở lệnh ngẫu nhiên
```
BUG:  Khi regime=-1 (T-KAN chưa đủ data), PPO vẫn infer và gửi LONG/SHORT.
FIX:  Guard trong SymbolWorker: nếu _regime == -1 → gửi HOLD, bỏ qua inference.
      File: mt5_bridge/runner.py  SymbolWorker.run()
```

---

## Key files

| File | Vai trò |
|------|---------|
| `config.py` | TẤT CẢ tham số — chỉ sửa ở đây |
| `risk/journal.py` | TradeRecord, TradeJournal — persistent per-symbol |
| `risk/kelly.py` | KellyPositionSizer (Bayesian, multi-scalar) + `reset_history()` |
| `risk/kill_switch.py` | KillSwitch — circuit breaker |
| `risk/news_filter.py` | NewsFilter — economic calendar blackout |
| `risk/sl_tp_optimizer.py` | SL/TP bucket optimizer — regime-aware TP multiplier |
| `mt5_bridge/runner.py` | SymbolWorker, StandardRiskAdapter, run_multi_live() |
| `mt5_bridge/signal_server.py` | ZMQ PUB, LiveStateWriter |
| `mt5_bridge/auto_retrainer.py` | AutoRetrainer — drift detection → PPO retrain + kelly.reset_history() |
| `mt5_bridge/XauDayTrader.mq5` | MQL5 EA: ZMQ SUB, Limit orders, partial TP, trail after TP, EOD close |
| `mt5_bridge/ATS_Panel.mq5` | MQL5 indicator: live state panel v2.31 |
| `ai_models/rl_agent.py` | PPO env, Sharpe reward, train_ppo / load_ppo |
| `ai_models/regime_tkan.py` | T-KAN classifier |
| `ai_models/features.py` | 24-dim feature vector, OU MLE |
| `data/pipeline.py` | MT5 fetch, Parquet cache, LiveTickStream |
| `backtest/walkforward.py` | Walk-forward, TCA |
| `backtest/validate_models.py` | OOS validation — 5 windows, Sharpe/WR/return pass criteria |
| `dashboard/app.py` | Streamlit — port 8501 |
| `retrain_all.py` | Batch retrain T-KAN + PPO cho tất cả symbols |
| `sync_mt5.sh` | Copy mt5_bridge/*.mq5 → đúng MT5 Experts/ & Indicators/ |

---

## Start / Stop

```bash
# Start full system
python start.py

# Dashboard only
python start.py --dashboard-only

# Retrain specific symbols
python retrain_all.py --symbols XAUUSD GBPUSD --bars 100000 --timesteps 500000

# Clear all models + cache + state, retrain from scratch
python -c "
from pathlib import Path
from config import MODEL_DIR, DATA_DIR, LOG_DIR
for d,pat in [(MODEL_DIR,'*'),(DATA_DIR,'*.parquet'),
              (LOG_DIR,'kelly_state_*.json'),(LOG_DIR,'trade_journal_*.json')]:
    for f in d.glob(pat): f.unlink()
"
python retrain_all.py --symbols XAUUSD GBPUSD BTCUSD
```

## State files
| File | Đọc bởi |
|------|---------|
| `logs/live_state.json` | Dashboard, ATS_Panel |
| `logs/worker_status.json` | Dashboard symbol tabs |
| `logs/trade_journal_{sym}.json` | Journal, peak equity recovery |
| `logs/kelly_state_{sym}.json` | Kelly restart recovery |
| `logs/economic_calendar.json` | NewsFilter (editable override) |
| `logs/runner.lock` | Single-instance guard |
