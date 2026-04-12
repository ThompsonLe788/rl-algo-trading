# Hướng Dẫn Sử Dụng: ATS XAU/USD

> **Dành cho ai?** Người mới bắt đầu sử dụng hệ thống, chưa cần biết về code nhưng cần hiểu cách vận hành hàng ngày.

---

## Mục Lục

1. [Yêu Cầu Hệ Thống](#1-yêu-cầu-hệ-thống)
2. [Cài Đặt Lần Đầu](#2-cài-đặt-lần-đầu)
3. [Cấu Hình](#3-cấu-hình)
4. [Khởi Động Hệ Thống](#4-khởi-động-hệ-thống)
5. [Sử Dụng Dashboard](#5-sử-dụng-dashboard)
6. [MT5: Gắn EA và Indicator](#6-mt5-gắn-ea-và-indicator)
7. [Thêm / Bớt Symbol](#7-thêm--bớt-symbol)
8. [Dừng Hệ Thống](#8-dừng-hệ-thống)
9. [Các Thao Tác Thường Gặp](#9-các-thao-tác-thường-gặp)
10. [Câu Hỏi Thường Gặp (FAQ)](#10-câu-hỏi-thường-gặp)

---

## 1. Yêu Cầu Hệ Thống

### Phần Cứng Tối Thiểu
| Thành phần | Yêu cầu tối thiểu | Khuyến nghị |
|---|---|---|
| CPU | 4 core | 8+ core (training nhanh hơn) |
| RAM | 8 GB | 16 GB (chạy nhiều symbol) |
| Disk | 10 GB trống | SSD 50 GB+ |
| Network | Kết nối ổn định (< 100ms latency đến broker) | Dây cáp, không WiFi |

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
# Từ terminal (Command Prompt hoặc PowerShell)
git clone https://github.com/your-repo/xau-ats.git d:\xau_ats
cd d:\xau_ats
```

Hoặc giải nén file ZIP vào `d:\xau_ats`.

### Bước 2: Tạo Virtual Environment

```bash
# Trong d:\xau_ats
python -m venv venv
venv\Scripts\activate
```

Sau khi activate, bạn thấy `(venv)` ở đầu dòng lệnh.

### Bước 3: Cài Thư Viện

```bash
pip install -r requirements.txt
```

Quá trình này mất 5-15 phút tùy tốc độ internet. Các thư viện chính:
- `MetaTrader5` — kết nối MT5
- `stable-baselines3` — PPO/SAC agent
- `torch` — PyTorch (T-KAN, neural networks)
- `zmq` — ZeroMQ messaging
- `streamlit` — dashboard web
- `python-telegram-bot` — Telegram alerts

### Bước 4: Cài ZeroMQ cho MT5

Tải `Experts/Libraries/libzmq.dll` cho MT5:
1. Tải từ: [github.com/dingmaotu/mql-zmq/releases](https://github.com/dingmaotu/mql-zmq/releases)
2. Copy `libzmq.dll` vào `%AppData%\MetaQuotes\Terminal\<ID>\MQL5\Libraries\`

### Bước 5: Copy File MQL5 vào MT5

```
Nguồn:       d:\xau_ats\mt5_bridge\ATS_Panel.mq5
             d:\xau_ats\mt5_bridge\ATS_StrategyView.mq5

Đích (chọn một trong hai):
  Option A:  %AppData%\MetaQuotes\Terminal\<ID>\MQL5\Experts\
  Option B:  Dùng MT5 menu: File → Open Data Folder → MQL5\Experts\
```

Sau khi copy, trong MT5: **F5** để compile, hoặc double-click file trong Navigator.

### Bước 6: Kiểm Tra Cài Đặt

```bash
# Trong d:\xau_ats với venv đang activate
python -c "import MetaTrader5; import zmq; import stable_baselines3; print('OK')"
```

Nếu in ra `OK` → cài đặt thành công.

---

## 3. Cấu Hình

### 3.1 File Cấu Hình Chính: `config.py`

Mở `d:\xau_ats\config.py` và điều chỉnh các tham số theo nhu cầu:

```python
# === Đường dẫn MT5 ===
# Tự động phát hiện, hoặc set thủ công nếu MT5 cài ở vị trí khác:
# MT5_FILES_PATH = Path(r"C:\Custom\MT5\Common\Files")

# === Risk Management ===
MAX_DRAWDOWN_PCT  = 15.0  # Kill switch khi drawdown > 15%
MAX_RISK_PER_TRADE = 0.02  # Rủi ro tối đa mỗi lệnh = 2% tài khoản
KELLY_FRACTION    = 0.1   # Dùng 10% Kelly (bảo thủ)
EOD_HOUR_GMT      = 22    # Đóng tất cả lệnh lúc 22h GMT

# === Kết Nối ===
ZMQ_SIGNAL_ADDR   = "tcp://127.0.0.1:5555"

# === Training ===
AUTO_TRAIN_BARS   = 50_000    # Số bar M1 để train
AUTO_TRAIN_TIMESTEPS = 200_000  # Bước training PPO
```

**Lưu ý:** Không cần sửa gì khác nếu bạn dùng cài đặt mặc định.

### 3.2 Telegram Alerts (Tùy Chọn)

Để nhận cảnh báo qua Telegram:

1. Tạo bot: nhắn tin `/newbot` cho [@BotFather](https://t.me/botfather) → lấy `TOKEN`
2. Get chat ID: nhắn `/start` cho bot, truy cập `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. Copy file `.env.example` thành `.env` và điền token:

```bash
copy .env.example .env
```

Nội dung `.env`:
```
TELEGRAM_TOKEN=123456789:ABCdefGHI-your-actual-token-here
TELEGRAM_CHAT_ID=123456789
```

**Quan trọng:** Không bao giờ điền token thẳng vào `config.py` — file `.env` được `.gitignore` bảo vệ, không bị commit lên GitHub.

### 3.3 Biến Môi Trường (Tuỳ Chọn)

File `.env` hỗ trợ tất cả biến môi trường:

```
TELEGRAM_TOKEN=your-token
TELEGRAM_CHAT_ID=your-chat-id
MT5_FILES_PATH=C:\Users\YourName\AppData\Roaming\MetaQuotes\Terminal\Common\Files
```

---

## 4. Khởi Động Hệ Thống

### Thứ Tự Khởi Động (Quan Trọng!)

```
1. Mở MT5 terminal và đăng nhập tài khoản
2. Khởi động Python backend (multi-runner)
3. Khởi động Streamlit dashboard
4. Gắn ATS_Panel EA vào chart trong MT5
```

### Bước 1: Mở MT5

Đăng nhập tài khoản (demo hoặc live). Đảm bảo có kết nối phía dưới cùng MT5.

### Bước 2: Khởi Động Backend

Mở **Terminal 1** (Command Prompt):

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

Để chạy mãi ngay cả khi terminal đóng, dùng PowerShell và `Start-Process`:
```powershell
Start-Process -WindowStyle Hidden python -ArgumentList "main.py multi-live"
```

### Bước 3: Khởi Động Dashboard

Mở **Terminal 2** (Command Prompt mới):

```bash
cd d:\xau_ats
venv\Scripts\activate
streamlit run dashboard/app.py
```

Mở trình duyệt → truy cập `http://localhost:8501`

### Bước 4: Gắn EA Vào Chart

1. Trong MT5, mở chart: `File → New Chart → XAUUSD` (hoặc bất kỳ symbol nào)
2. Chuyển timeframe sang **M1** (khuyến nghị, nhưng không bắt buộc — EA hoạt động ở mọi TF)
3. Trong **Navigator** → **Expert Advisors** → kéo `ATS_Panel` vào chart
4. Trong cửa sổ cài đặt:
   - **Allow algo trading**: ✅ tick
   - **Allow DLL imports**: ✅ tick (cần cho ZMQ)
5. Click OK — EA sẽ hiện lên góc trên phải chart

Sau vài giây, trong Terminal 1 bạn sẽ thấy:
```
INFO [multi_runner] New chart detected: XAUUSD — starting worker
INFO [XAUUSD] status → waiting
INFO [XAUUSD] Fetching 50000 bars from MT5...
INFO [XAUUSD] Training PPO on 50000 bars, 200000 timesteps...
```

**Lần đầu training mất 3-5 phút.** Các lần tiếp theo tải model đã train ngay lập tức.

---

## 5. Sử Dụng Dashboard

Truy cập `http://localhost:8501` để xem dashboard.

### 5.1 Sidebar

| Phần | Mô tả |
|---|---|
| **Status badge** | 🟢 LIVE (đang chạy) / 🔴 KILLED (kill switch kích hoạt) / ⚪ OFFLINE (không có dữ liệu) |
| **Equity** | Số dư tài khoản hiện tại |
| **Balance** | Số dư gốc |
| **Session P&L** | Lãi/lỗ phiên hiện tại |
| **Drawdown gauge** | Gauge màu: xanh (< 5%), cam (5-10%), đỏ (> 10%) |
| **Heartbeat** | Thời gian kể từ tín hiệu cuối. Nếu > 30s → cần kiểm tra |

### 5.2 Tabs Theo Symbol

Mỗi symbol đang active hiển thị một tab:

| Field | Ý nghĩa |
|---|---|
| **Worker status** | ⏳ training / 🟢 live / ⚪ waiting / 🔴 error |
| **Regime** | RANGE (đi ngang) hoặc TREND (có xu hướng) |
| **Position** | Vị thế hiện tại: LONG / SHORT / FLAT |
| **Entry price** | Giá vào lệnh |
| **Unrealized P&L** | Lãi/lỗ chưa chốt |
| **Kelly f*** | Tỷ lệ vốn Kelly hiện tại |
| **Equity curve** | Biểu đồ equity trong phiên |
| **Log** | 100 dòng log gần nhất liên quan symbol |

### 5.3 Tab System

Hiển thị:
- Trạng thái kill switch
- Tổng số tín hiệu đã gửi
- Lý do kill (nếu có)
- Worker status của tất cả symbols

### 5.4 Auto-Refresh

Dashboard tự refresh mỗi **5 giây**. Không cần F5 thủ công.

---

## 6. MT5: Gắn EA và Indicator

### 6.1 ATS_Panel (EA — Đặt Lệnh)

EA này nhận tín hiệu từ Python và đặt lệnh thật sự.

**Các tham số quan trọng khi gắn:**

| Tham số | Mặc định | Mô tả |
|---|---|---|
| `ZmqAddr` | `tcp://127.0.0.1:5555` | Địa chỉ ZMQ kết nối Python |
| `Symbol` | Tự động | Symbol của chart (không cần đổi) |
| `MagicNumber` | `20240101` | ID để phân biệt lệnh ATS với lệnh thủ công |
| `MaxSlippage` | `10` | Slippage tối đa chấp nhận (points) |
| `FileSignalFallback` | `true` | Dùng file JSON nếu ZMQ ngắt |

**Kiểm tra EA hoạt động:** Góc phải trên chart có icon mặt cười 😊 (allow trading) không có dấu X màu đỏ.

### 6.2 ATS_StrategyView (Indicator — Chỉ Nhìn)

Indicator này hiển thị chiến lược lên biểu đồ (không đặt lệnh). Thêm vào cùng chart với EA.

```
Navigator → Indicators → ATS_StrategyView → kéo vào chart
```

**Cái bạn sẽ thấy:**
- **Đường xanh (VWAP)**: Giá trung bình có trọng số volume trong ngày
- **Đường cam chấm (ATR bands)**: Vùng ±1.5×ATR từ VWAP
- **Mũi tên xanh lá (↑)**: Tín hiệu Long từ AI
- **Mũi tên đỏ (↓)**: Tín hiệu Short từ AI
- **Vòng tròn vàng (○)**: Đóng vị thế
- **Subwindow dưới**: Z-score (magenta) — dưới -2 → oversold, trên +2 → overbought
- **Màu nền**: Xám = RANGE regime, xanh nhạt = TREND regime

**Tham số ATS_StrategyView:**

| Tham số | Mặc định | Mô tả |
|---|---|---|
| `ZScoreWindow` | 50 | Cửa sổ tính z-score (bars) |
| `ATRPeriod` | 14 | Chu kỳ ATR |
| `ATRMultSL` | 1.5 | Nhân số ATR cho bands |
| `RefreshSec` | 2 | Tần suất đọc file tín hiệu (giây) |

---

## 7. Thêm / Bớt Symbol

### Thêm Symbol Mới

1. Trong MT5 → mở chart symbol mới (ví dụ: EURUSD)
2. Gắn `ATS_Panel` EA vào chart đó (như bước 4 ở trên)
3. Python multi-runner tự động phát hiện trong ≤ 5 giây
4. Worker mới khởi động → tự train model cho EURUSD nếu chưa có

**Không cần restart bất kỳ thứ gì!** Multi-runner scan tự động.

### Bớt / Tạm Dừng Symbol

Chỉ cần **xóa EA** khỏi chart hoặc **đóng chart**:
- EA OnDeinit ghi `ats_chart_EURUSD.txt = "0"`
- Multi-runner phát hiện trong ≤ 5 giây → dừng worker EURUSD
- Model đã train vẫn còn lưu, lần sau gắn lại sẽ load ngay

### Symbols Được Hỗ Trợ

Về lý thuyết, bất kỳ symbol nào MT5 hỗ trợ đều hoạt động. Đã test thực tế:
- ✅ XAUUSD (tốt nhất, đây là mục tiêu chính)
- ✅ EURUSD
- ✅ BTCUSD / BTCUSDT

**Lưu ý:** Model train riêng cho từng symbol. XAUUSD model không dùng được cho EURUSD.

---

## 8. Dừng Hệ Thống

### Dừng Đúng Cách

**Bước 1:** Trong MT5 → xóa tất cả EA khỏi các chart (chuột phải → Remove Expert)

**Bước 2:** Trong Terminal 1 (multi-runner) → nhấn `Ctrl+C`:
```
INFO [multi_runner] Shutting down multi-runner...
INFO [multi_runner] All workers stopped.
```

**Bước 3:** Trong Terminal 2 (streamlit) → nhấn `Ctrl+C`

### Dừng Khẩn Cấp

Nếu không thể dừng bình thường:

```bash
# Tìm và kill process Python
taskkill /f /im python.exe

# Hoặc mở Task Manager → tìm python.exe → End Task
```

**Quan trọng:** Sau khi kill Python, mọi lệnh đang mở trong MT5 vẫn còn (MT5 quản lý độc lập). Kiểm tra và đóng thủ công nếu cần.

### Kill Switch Tự Động

Hệ thống tự động dừng giao dịch khi:
- Drawdown > 15% → `status = "killed"`, dashboard hiện 🔴
- Telegram nhận cảnh báo: "⚠️ Kill switch activated"

Để reset sau kill switch:
```bash
python main.py reset-kill
```
Hoặc khởi động lại toàn bộ hệ thống.

---

## 9. Các Thao Tác Thường Gặp

### Train Lại Model

Khi muốn train lại model với dữ liệu mới nhất:

```bash
# Train lại XAUUSD
python main.py train --symbol XAUUSD --bars 50000

# Train lại với dữ liệu tổng hợp (không cần MT5)
python main.py train --symbol XAUUSD --bars 50000 --synthetic
```

### Xem Log

```bash
# Log giao dịch real-time
type d:\xau_ats\logs\signal_server.log

# Theo dõi liên tục (PowerShell)
Get-Content d:\xau_ats\logs\signal_server.log -Wait -Tail 50
```

### Chạy Backtest

```bash
# Walk-forward backtest trên XAUUSD, 3 tháng
python main.py backtest --symbol XAUUSD --months 3
```

Kết quả xuất ra màn hình gồm: Sharpe ratio, max drawdown, win rate, profit factor theo từng fold.

### Kiểm Tra Trạng Thái Workers

```bash
type d:\xau_ats\logs\worker_status.json
```

Output ví dụ:
```json
{
  "XAUUSD": "live",
  "EURUSD": "training",
  "BTCUSD": "waiting"
}
```

### Xem Model Đã Có

```bash
dir d:\xau_ats\ai_models\checkpoints\
```

Output ví dụ:
```
ppo_xauusd.zip   (45 MB)
ppo_eurusd.zip   (44 MB)
```

### Chạy Test Suite

```bash
python -m pytest tests/ -v
```

Tất cả 38 tests phải pass. Nếu có test fail sau khi chỉnh code → không deploy.

---

## 10. Câu Hỏi Thường Gặp

### "Worker gặp lỗi" trên dashboard

**Bước 1:** Xem log để tìm nguyên nhân:
```bash
type d:\xau_ats\logs\signal_server.log | findstr "ERROR"
```

**Nguyên nhân thường gặp:**
| Lỗi | Nguyên nhân | Giải pháp |
|---|---|---|
| `MT5 init failed` | MT5 chưa mở hoặc chưa đăng nhập | Mở MT5, đăng nhập |
| `Address in use` | Port 5555 đang bị chiếm | Tắt process Python cũ, restart |
| `No bars returned` | MT5 không có dữ liệu symbol này | Kiểm tra symbol in MT5, tải history |
| `Auto-train failed` | Thiếu thư viện hoặc RAM | Pip install lại, kiểm tra RAM |

### "Heartbeat > 30s" trên dashboard

Python backend không gửi được tín hiệu. Kiểm tra:
1. Terminal 1 (multi-runner) còn đang chạy không?
2. Có lỗi gì trong log không?
3. Restart multi-runner nếu cần

### MT5 không nhận được tín hiệu

1. Kiểm tra EA có icon 😊 không (phải là mặt cười, không phải ☹️)
2. MT5 → Tools → Options → Expert Advisors → tick "Allow automated trading"
3. Kiểm tra `libzmq.dll` đã copy vào đúng thư mục MQL5\Libraries\ chưa

### Lệnh không được đặt dù có tín hiệu

1. Trong MT5, kiểm tra tab **Journal** — xem có lỗi gì không
2. Kiểm tra broker cho phép auto trading (một số broker yêu cầu xác minh)
3. Kiểm tra tài khoản có đủ margin không

### Cách biết AI đang ra quyết định gì?

Dashboard → tab symbol → xem **Position** và **Last Signal** (nếu có). Hoặc xem chart với `ATS_StrategyView` để thấy mũi tên tín hiệu và z-score.

### Model có thể đặt lệnh ngược chiều nhau không?

Không trong cùng một symbol. Mỗi symbol chỉ có một vị thế tại một thời điểm (Long, Short, hoặc Flat). Agent không mở thêm lệnh khi đã có vị thế — phải đóng (action=3 "Close") trước khi đảo chiều.

### Bao lâu thì train lại model?

Không có lịch cố định, nhưng khuyến nghị:
- **Hàng tuần:** Kiểm tra win rate trong log. Nếu < 45% → cân nhắc retrain
- **Mỗi tháng:** Retrain với 50.000 bar mới nhất
- **Sau sự kiện lớn** (Fed meeting, financial crisis): Retrain ngay

### Có thể chạy 24/7 không?

Có, nhưng lưu ý:
- Hệ thống tự đóng lệnh lúc **22h GMT** mỗi ngày
- Không mở lệnh mới sau **21h GMT**
- Cuối tuần (thứ 7-chủ nhật): Vàng đóng cửa, không có tick → worker ở trạng thái chờ
- Nên restart hệ thống mỗi sáng thứ Hai để clear cache

### Mất điện hoặc crash giữa chừng?

Lệnh đang mở trong MT5 vẫn còn (MT5 quản lý độc lập với Python). Khi restart:
1. Kiểm tra MT5: có lệnh nào còn mở không?
2. Khởi động lại multi-runner
3. Gắn lại EA vào charts
4. Hệ thống tiếp tục bình thường — `live_state.json` sẽ sync lại trong < 30 giây

---

## Phụ Lục: Shortcut Thường Dùng

| Lệnh | Mô tả |
|---|---|
| `python main.py multi-live` | Khởi động backend đa symbol |
| `streamlit run dashboard/app.py` | Khởi động dashboard |
| `python main.py train --symbol XAUUSD` | Train model XAUUSD |
| `python main.py backtest --symbol XAUUSD` | Chạy backtest |
| `python -m pytest tests/ -v` | Chạy tất cả tests |
| `python main.py reset-kill` | Reset kill switch |
| `type logs\signal_server.log` | Xem log giao dịch |
| `type logs\worker_status.json` | Xem trạng thái workers |
