# Hướng Dẫn Sử Dụng: ATS XAU/USD

> **Dành cho ai?** Người mới bắt đầu sử dụng hệ thống, chưa cần biết về code nhưng cần hiểu cách vận hành hàng ngày.

---

## Mục Lục

1. [Yêu Cầu Hệ Thống](#1-yêu-cầu-hệ-thống)
2. [Cài Đặt Lần Đầu](#2-cài-đặt-lần-đầu)
3. [Cấu Hình](#3-cấu-hình)
4. [Khởi Động Hệ Thống](#4-khởi-động-hệ-thống)
5. [Sử Dụng ATS_Panel trên MT5](#5-sử-dụng-ats_panel-trên-mt5)
6. [Sử Dụng Dashboard Web](#6-sử-dụng-dashboard-web)
7. [Thêm / Bớt Symbol](#7-thêm--bớt-symbol)
8. [Auto-Retrain (Tự Học)](#8-auto-retrain-tự-học)
9. [Dừng Hệ Thống](#9-dừng-hệ-thống)
10. [Các Thao Tác Thường Gặp](#10-các-thao-tác-thường-gặp)
11. [Câu Hỏi Thường Gặp (FAQ)](#11-câu-hỏi-thường-gặp)

---

## 1. Yêu Cầu Hệ Thống

### Phần Cứng Tối Thiểu
| Thành phần | Yêu cầu tối thiểu | Khuyến nghị |
|---|---|---|
| CPU | 4 core | 8+ core (training nhanh hơn) |
| RAM | 8 GB | 16 GB (chạy nhiều symbol) |
| Disk | 10 GB trống | SSD 50 GB+ |
| Network | Kết nối ổn định (< 100ms đến broker) | Dây cáp, không WiFi |

### Phần Mềm
- **Windows 10/11** (64-bit) — MT5 chỉ hỗ trợ Windows chính thức
- **MetaTrader 5** — tải miễn phí từ trang broker
- **Python 3.11 hoặc 3.12** — [python.org/downloads](https://www.python.org/downloads/)
- **Git** (tùy chọn) — để clone repo

### Tài Khoản
- Tài khoản MT5 tại broker hỗ trợ XAU/USD (ví dụ: IC Markets, Pepperstone, Exness)
- **Demo account** trước, **live account** sau khi đã kiểm tra kỹ

---

## 2. Cài Đặt Lần Đầu

### Bước 1: Tải Source Code

```bash
git clone https://github.com/your-repo/xau-ats.git d:\xau_ats
cd d:\xau_ats
```

Hoặc giải nén file ZIP vào `d:\xau_ats`.

### Bước 2: Tạo Virtual Environment

```bash
cd d:\xau_ats
python -m venv venv
venv\Scripts\activate
```

Sau khi activate, bạn thấy `(venv)` ở đầu dòng lệnh.

### Bước 3: Cài Thư Viện

```bash
pip install -r requirements.txt
```

Quá trình này mất 5–15 phút. Các thư viện chính:
- `MetaTrader5` — kết nối MT5
- `stable-baselines3` — PPO/SAC agent
- `torch` — PyTorch (T-KAN, neural networks)
- `zmq` — ZeroMQ messaging
- `streamlit` — dashboard web
- `python-telegram-bot` — Telegram alerts

### Bước 4: Cài ZeroMQ cho MT5

1. Tải từ: [github.com/dingmaotu/mql-zmq/releases](https://github.com/dingmaotu/mql-zmq/releases)
2. Copy `libzmq.dll` vào:
   ```
   %AppData%\MetaQuotes\Terminal\<ID>\MQL5\Libraries\
   ```

### Bước 5: Copy File MQL5 vào MT5

Hệ thống gồm **2 file MQL5** với vai trò khác nhau:

| File | Loại | Thư mục đích | Vai trò |
|---|---|---|---|
| `ATS_Panel.mq5` | **Indicator** | `MQL5\Indicators\` | Hiển thị trạng thái + giao tiếp Python |
| `XauDayTrader.mq5` | **Expert Advisor** | `MQL5\Experts\` | Nhận tín hiệu ZMQ, đặt lệnh thật |

```
Nguồn:  d:\xau_ats\mt5_bridge\ATS_Panel.mq5
Đích:   %AppData%\MetaQuotes\Terminal\<ID>\MQL5\Indicators\

Nguồn:  d:\xau_ats\mt5_bridge\XauDayTrader.mq5
Đích:   %AppData%\MetaQuotes\Terminal\<ID>\MQL5\Experts\
```

Sau khi copy, trong MT5: **F5** để compile, hoặc double-click file trong Navigator.

### Bước 6: Kiểm Tra Cài Đặt

```bash
python -c "import MetaTrader5; import zmq; import stable_baselines3; print('OK')"
```

Nếu in ra `OK` → cài đặt thành công.

---

## 3. Cấu Hình

### 3.1 File Cấu Hình Chính: `config.py`

```python
# === Risk Management ===
MAX_DRAWDOWN_PCT   = 15.0   # Kill switch khi drawdown > 15%
MAX_RISK_PER_TRADE = 0.02   # Rủi ro tối đa mỗi lệnh = 2% tài khoản
KELLY_FRACTION     = 0.1    # Dùng 10% Kelly (bảo thủ)
EOD_HOUR_GMT       = 22     # Đóng tất cả lệnh lúc 22h GMT

# === Kết Nối ===
ZMQ_SIGNAL_ADDR    = "tcp://127.0.0.1:5555"

# === Training ===
AUTO_TRAIN_BARS      = 50_000    # Số bar M1 để train lần đầu
AUTO_TRAIN_TIMESTEPS = 200_000   # Bước training PPO lần đầu
RETRAIN_TIMESTEPS    = 300_000   # Bước training PPO khi retrain
```

**Lưu ý:** Không cần sửa gì khác nếu dùng cài đặt mặc định.

### 3.2 Telegram Alerts (Tùy Chọn)

1. Tạo bot: nhắn `/newbot` cho [@BotFather](https://t.me/botfather) → lấy TOKEN
2. Copy `.env.example` thành `.env` và điền:

```
TELEGRAM_TOKEN=123456789:ABCdef-your-token
TELEGRAM_CHAT_ID=123456789
```

**Quan trọng:** Không điền token vào `config.py` — file `.env` được `.gitignore` bảo vệ.

---

## 4. Khởi Động Hệ Thống

### Thứ Tự Khởi Động (Quan Trọng!)

```
1. Mở MT5 → đăng nhập tài khoản
2. Khởi động Python backend (multi-runner)
3. Khởi động Streamlit dashboard
4. Trong MT5: gắn XauDayTrader EA + ATS_Panel Indicator vào chart
```

### Bước 1: Mở MT5

Đăng nhập tài khoản. Đảm bảo có chữ "Connected" ở góc phải dưới MT5.

### Bước 2: Khởi Động Backend

Mở **Terminal 1**:

```bash
cd d:\xau_ats
venv\Scripts\activate
python main.py multi-live
```

Bạn sẽ thấy:
```
INFO [multi_runner] Multi-runner started. Scanning ... every 5s
INFO [multi_runner] No active charts detected yet — waiting...
```

Để chạy ẩn nền (PowerShell):
```powershell
Start-Process -WindowStyle Hidden python -ArgumentList "main.py multi-live"
```

### Bước 3: Khởi Động Dashboard

Mở **Terminal 2**:

```bash
cd d:\xau_ats
venv\Scripts\activate
streamlit run dashboard/app.py
```

Truy cập `http://localhost:8501` bằng trình duyệt.

### Bước 4: Gắn Vào Chart MT5

Trong MT5, với mỗi chart symbol muốn giao dịch:

1. Mở chart: `File → New Chart → XAUUSD`
2. **Gắn EA** (đặt lệnh): Navigator → **Expert Advisors** → kéo `XauDayTrader` vào chart
   - **Allow algo trading**: ✅
   - **Allow DLL imports**: ✅ (cần cho ZMQ)
3. **Gắn Indicator** (hiển thị): Navigator → **Indicators** → kéo `ATS_Panel` vào chart
   - Panel hiện ra ở **góc trên trái** của chart

Sau vài giây, Terminal 1 sẽ hiện:
```
INFO [multi_runner] New chart detected: XAUUSD — starting worker
INFO [XAUUSD] Model loaded
INFO [XAUUSD] status → live
```

**Lần đầu tiên (chưa có model):**
```
INFO [XAUUSD] No model found — starting auto-train
INFO [XAUUSD] Training PPO on 50000 bars, 200000 timesteps...
```
→ Chờ **3–5 phút**. Sau đó tự động chuyển sang "live".

---

## 5. Sử Dụng ATS_Panel trên MT5

ATS_Panel là indicator hiển thị trạng thái toàn bộ hệ thống ngay trên chart, **góc trên trái**.

### Các Section

```
┌─────────────────────────────────┐
│ ATS PANEL — XAUUSD              │
├─────────────────────────────────┤
│ MARKET                          │
│  Chart TF: M1  │ ATS TF: M1(RL) │
│  Session: London│ Spread: 0.8bp  │
├─────────────────────────────────┤
│ POSITION                        │
│  Status: LONG  │ Regime: RANGE  │
│  Entry: 3200.50                 │
│  Unrealized: +12.30             │
│  Sym DD: 0.5%  │ Acct DD: 0.5%  │
├─────────────────────────────────┤
│ SIGNALS (last 5)                │
│  Dir │ Price  │Win%│R/R│Lot│Time│
│  BUY │3200.50 │57% │1.8│0.1│08:32│
│  ...                            │
│  SL: 3192.00  TP: 3215.00       │
│  Z-Score: -1.82                 │
├─────────────────────────────────┤
│ RISK                            │
│  Kelly f: 1.67%                 │
│  Equity: 10,120 │Balance: 10,000│
├─────────────────────────────────┤
│ AI MODEL                        │
│  Version: ppo_xauusd [04-15 23:32]│
│  Status: Running                │
│  Last Train: ---                │
│  Reason: ---                    │
│  WinRate: 55.0%│ Sharpe: 1.23   │
│  Trades: 34                     │
├─────────────────────────────────┤
│ SYSTEM                          │
│  Kill: ---                      │
│  Heartbeat: 3s ago              │
│  Updated: 10:38:02              │
└─────────────────────────────────┘
```

### Ý Nghĩa Các Section

**MARKET** — thông tin thị trường hiện tại
| Field | Ý nghĩa |
|---|---|
| Chart TF | Timeframe của chart đang xem |
| ATS TF | Timeframe AI phân tích (M1 RL = M1 bar với PPO) |
| Session | Phiên giao dịch hiện tại (Tokyo / London / NY / Off) |
| Spread | Spread hiện tại (basis points) |

**POSITION** — vị thế đang mở
| Field | Ý nghĩa |
|---|---|
| Status | LONG / SHORT / FLAT |
| Regime | RANGE (đi ngang) / TREND (có xu hướng) |
| Entry | Giá vào lệnh |
| Unrealized | Lãi/lỗ chưa chốt (USD) |
| Sym DD | Drawdown tính theo symbol này |
| Acct DD | Drawdown tài khoản tổng |

**SIGNALS** — 5 tín hiệu gần nhất
| Cột | Ý nghĩa |
|---|---|
| Dir | BUY / SELL / CLOSE |
| Price | Giá lúc phát tín hiệu |
| Win% | Xác suất thắng theo AI |
| R/R | Tỷ lệ risk/reward |
| Lot | Kích thước lệnh |
| Time | Giờ phát tín hiệu (GMT) |

**AI MODEL** — thông tin model đang dùng
| Field | Ý nghĩa |
|---|---|
| Version | Tên model + ngày train |
| Status | Running / **TRAINING** (đang retrain) |
| Last Train | Thời điểm retrain gần nhất |
| Reason | Lý do retrain (weekly / drift) |
| WinRate | Tỷ lệ thắng rolling 50 lệnh |
| Sharpe | Chỉ số Sharpe rolling |
| Trades | Tổng số lệnh đã thực hiện |

**SYSTEM** — trạng thái hệ thống
| Field | Ý nghĩa |
|---|---|
| Kill | Lý do kill switch (nếu kích hoạt) |
| Heartbeat | Thời gian từ nhịp tim cuối (< 15s = bình thường) |
| Updated | Thời gian JSON cập nhật lần cuối |

### Màu Sắc Trạng Thái

| Màu | Ý nghĩa |
|---|---|
| Xanh lá | Hoạt động bình thường / LONG |
| Đỏ | Cảnh báo / SHORT / lỗi |
| Vàng | Đang xử lý / đang train |
| Trắng | Neutral / FLAT |

---

## 6. Sử Dụng Dashboard Web

Truy cập `http://localhost:8501`.

### Sidebar

| Phần | Mô tả |
|---|---|
| **Status badge** | 🟢 LIVE / 🔴 KILLED / ⚪ OFFLINE |
| **Equity / Balance** | Số dư tài khoản |
| **Session P&L** | Lãi/lỗ phiên hiện tại |
| **Drawdown gauge** | Xanh (< 5%), cam (5–10%), đỏ (> 10%) |
| **Heartbeat** | Nếu > 30s → kiểm tra backend |

### Tabs Theo Symbol

| Field | Ý nghĩa |
|---|---|
| **Worker status** | ⏳ training / 🟢 live / ⚪ waiting / 🔴 error |
| **Regime** | RANGE / TREND |
| **Position** | LONG / SHORT / FLAT |
| **Model version** | Tên + ngày train |
| **Win rate / Sharpe** | Hiệu suất rolling |
| **Equity curve** | Biểu đồ trong phiên |
| **Log** | 100 dòng log gần nhất |

Dashboard tự refresh mỗi **5 giây**.

---

## 7. Thêm / Bớt Symbol

### Thêm Symbol Mới

1. Trong MT5 → mở chart symbol mới (ví dụ: EURUSD)
2. Gắn `XauDayTrader` EA + `ATS_Panel` Indicator vào chart đó
3. Python multi-runner tự động phát hiện trong ≤ 5 giây
4. Worker mới khởi động → tự train nếu chưa có model

**Không cần restart bất kỳ thứ gì.** Multi-runner scan tự động.

### Bớt / Tạm Dừng Symbol

Chỉ cần **xóa ATS_Panel** khỏi chart hoặc **đóng chart**:
- ATS_Panel ghi `ats_chart_EURUSD.txt = "0"` khi bị gỡ
- Multi-runner phát hiện trong ≤ 5 giây → dừng worker EURUSD
- Model đã train vẫn lưu, lần sau gắn lại sẽ load ngay

### Symbols Được Hỗ Trợ

| Symbol | Tình trạng | Ghi chú |
|---|---|---|
| XAUUSD | ✅ Ưu tiên | Mục tiêu chính, cấu hình tối ưu |
| EURUSD | ✅ Đã test | spread 0.3bp, leverage 500 |
| GBPUSD | ✅ Đã test | spread 0.4bp, leverage 500 |
| USDJPY | ✅ Đã test | spread 0.3bp, 3 decimals |
| BTCUSD | ✅ Đã test | spread cao hơn, leverage 100 |
| NAS100 | ✅ Đã test | EOD 21h GMT |
| Khác | ⚠️ Chưa test | Cần cấu hình trong `config.py` |

**Lưu ý:** Model train riêng cho từng symbol. XAUUSD model không dùng được cho EURUSD.

---

## 8. Auto-Retrain (Tự Học)

Hệ thống **tự động retrain** mà không cần can thiệp thủ công.

### Khi Nào Tự Retrain?

| Điều kiện | Chi tiết |
|---|---|
| **Weekly schedule** | Mỗi thứ Hai, một lần/tuần tự động |
| **Drift detection** | Win rate < 43% trong chuỗi lệnh gần nhất |

### Quá Trình Retrain (Tự Động)

```
1. [Phát hiện trigger] → AutoRetrainer khởi động training ngầm
2. [Panel hiển thị]    → AI MODEL → Status: "TRAINING"
3. [Training xong]     → Đánh giá model mới vs model cũ (Sharpe)
4. [Nếu model mới tốt hơn] → Tự động swap, model cũ backup
5. [Nếu model mới kém hơn] → Giữ nguyên model cũ
6. [Panel cập nhật]    → Version mới, Last Train, Reason
```

Trong lúc retrain, **live trading tiếp tục bình thường** với model hiện tại.

### Theo Dõi Quá Trình Retrain

**Trên ATS_Panel (MT5):**
- `Status: TRAINING` → đang train
- `Status: Running` → đã xong

**Trên Dashboard:**
- Worker status: `retraining`

**Trong log:**
```bash
Get-Content d:\xau_ats\logs\signal_server.log -Tail 30 | Select-String "AutoRetrain"
```

### Retrain Thủ Công

Nếu muốn retrain ngay (không chờ lịch tự động):

```bash
# Dừng worker: xóa ATS_Panel khỏi chart trong MT5

# Train lại với dữ liệu mới nhất
python main.py train --symbol XAUUSD --bars 50000

# Gắn lại ATS_Panel vào chart → worker load model mới
```

---

## 9. Dừng Hệ Thống

### Dừng Đúng Cách

**Bước 1:** Trong MT5 → xóa ATS_Panel khỏi các chart (chuột phải → Remove Indicator)

**Bước 2:** Terminal 1 → `Ctrl+C`:
```
INFO [multi_runner] Shutting down multi-runner...
INFO [multi_runner] All workers stopped.
```

**Bước 3:** Terminal 2 (Streamlit) → `Ctrl+C`

### Dừng Khẩn Cấp

```bash
taskkill /f /im python.exe
```

**Quan trọng:** Sau khi kill Python, lệnh đang mở trong MT5 vẫn còn. Kiểm tra và đóng thủ công nếu cần.

### Kill Switch Tự Động

Hệ thống tự dừng giao dịch khi:
- Drawdown > **15%** → `status = "killed"`, dashboard 🔴
- Telegram: "⚠️ Kill switch activated"

Reset sau kill switch:
```bash
python main.py reset-kill
```

---

## 10. Các Thao Tác Thường Gặp

### Kiểm Tra Trạng Thái Nhanh

```bash
# Worker status
type d:\xau_ats\logs\worker_status.json

# Live state (equity, positions, model info)
python -c "
import json
from pathlib import Path
d = json.loads(Path('logs/live_state.json').read_text())
for sym in ['XAUUSD','EURUSD']:
    s = d.get(sym, {})
    print(f'{sym}: pos={s.get(\"position\")} model={s.get(\"model_version\",\"?\")} wr={s.get(\"win_rate\",0):.0%}')
"
```

### Xem Log Real-Time

```powershell
# Theo dõi liên tục (PowerShell)
Get-Content d:\xau_ats\logs\signal_server.log -Wait -Tail 50

# Chỉ xem lỗi
Select-String -Path d:\xau_ats\logs\signal_server.log -Pattern "ERROR|CRITICAL"

# Xem tín hiệu giao dịch
Select-String -Path d:\xau_ats\logs\signal_server.log -Pattern "Published"
```

### Chạy Backtest

```bash
python main.py backtest --symbol XAUUSD --months 3
```

Kết quả: Sharpe ratio, max drawdown, win rate, profit factor theo từng fold.

### Xem Model Đã Có

```bash
dir d:\xau_ats\ai_models\checkpoints\
```

Output mẫu:
```
ppo_xauusd.zip          (45 MB) — 2026-04-15 23:32
ppo_eurusd.zip          (44 MB) — 2026-04-13 08:03
ppo_xauusd_backup_20260413_0830.zip  — bản backup trước retrain
```

### Chạy Test Suite

```bash
python -m pytest tests/ -v
```

Tất cả tests phải pass. Nếu fail sau khi chỉnh code → không deploy.

---

## 11. Câu Hỏi Thường Gặp

### Panel ATS_Panel không hiện trên chart?

1. Đảm bảo copy file vào **`MQL5\Indicators\`** (không phải `MQL5\Experts\`)
2. Trong MT5: F5 để compile lại
3. Navigator → Indicators → tìm `ATS_Panel` → kéo vào chart
4. Kiểm tra không có lỗi trong MT5 → View → Terminal → Experts tab

### Section AI MODEL hoặc SIGNALS trống?

Python backend chưa ghi dữ liệu. Kiểm tra:
```bash
# Backend có chạy không?
tasklist | findstr python

# Worker status
type d:\xau_ats\logs\worker_status.json
```
Nếu không có Python process → `python main.py multi-live`

Nếu đang chạy nhưng vẫn trống → chờ 15 giây (1 heartbeat cycle).

### "Worker gặp lỗi" trên dashboard?

```bash
type d:\xau_ats\logs\signal_server.log | findstr "ERROR"
```

| Lỗi | Nguyên nhân | Giải pháp |
|---|---|---|
| `MT5 init failed` | MT5 chưa mở hoặc chưa đăng nhập | Mở MT5, đăng nhập |
| `Address in use` | Port 5555 bị chiếm | Tắt Python cũ, restart |
| `No bars returned` | MT5 không có dữ liệu symbol | Kiểm tra symbol, tải history |
| `Auto-train failed` | Thiếu thư viện hoặc RAM | Pip install lại |

### "Heartbeat > 30s" trên dashboard?

Backend không cập nhật. Kiểm tra:
1. Terminal 1 (multi-runner) còn chạy không?
2. Có lỗi trong log không?
3. Restart multi-runner nếu cần

### MT5 không nhận được tín hiệu / không đặt lệnh?

1. Kiểm tra `XauDayTrader` EA có icon 😊 không (phải là mặt cười, không ☹️)
2. MT5 → Tools → Options → Expert Advisors → tick "Allow automated trading"
3. Kiểm tra `libzmq.dll` đã copy vào `MQL5\Libraries\` chưa
4. Kiểm tra MT5 Journal tab xem có lỗi gì không

### AI đang train — có giao dịch không?

Có. Trong lúc auto-retrain:
- Model cũ vẫn chạy giao dịch bình thường
- Panel hiển thị `Status: TRAINING`
- Khi xong, nếu model mới tốt hơn → tự swap không gián đoạn

### Bao lâu auto-retrain một lần?

- **Hàng tuần (thứ Hai):** Luôn luôn, tự động
- **Drift detection:** Ngay khi win rate < 43% trong chuỗi lệnh gần đây (minimum 30 lệnh)
- Giữa hai lần drift retrain có cooldown **24 giờ** (tránh thrashing)

### Mất điện hoặc crash giữa chừng?

Lệnh đang mở trong MT5 vẫn còn (MT5 quản lý độc lập). Khi restart:
1. Kiểm tra MT5: có lệnh nào còn mở không?
2. Khởi động lại `python main.py multi-live`
3. Gắn lại ATS_Panel Indicator + XauDayTrader EA vào charts
4. Hệ thống sync lại trong < 30 giây

### Có thể chạy 24/7 không?

Có, nhưng lưu ý:
- Tự đóng lệnh lúc **22h GMT** mỗi ngày
- Không mở lệnh mới sau **21h GMT**
- Cuối tuần: Vàng đóng cửa, worker ở trạng thái chờ
- Khuyến nghị: restart hệ thống mỗi sáng thứ Hai

---

## Phụ Lục: Shortcut Thường Dùng

| Lệnh | Mô tả |
|---|---|
| `python main.py multi-live` | Khởi động backend đa symbol |
| `streamlit run dashboard/app.py` | Khởi động dashboard |
| `python main.py train --symbol XAUUSD` | Train thủ công XAUUSD |
| `python main.py backtest --symbol XAUUSD` | Chạy backtest |
| `python -m pytest tests/ -v` | Chạy tất cả tests |
| `python main.py reset-kill` | Reset kill switch |
| `type logs\signal_server.log` | Xem log giao dịch |
| `type logs\worker_status.json` | Xem trạng thái workers |
| `type logs\live_state.json` | Xem trạng thái live đầy đủ |
