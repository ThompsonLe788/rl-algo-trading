# Sổ Tay Vận Hành: ATS XAU/USD

> **Dành cho ai?** Người chịu trách nhiệm vận hành hệ thống hàng ngày — bao gồm khởi động/tắt theo lịch, giám sát hiệu suất, ứng phó sự cố, và bảo trì định kỳ.

---

## Mục Lục

1. [Kiến Trúc Hệ Thống](#1-kiến-trúc-hệ-thống)
2. [Quy Trình Khởi Động Hàng Ngày](#2-quy-trình-khởi-động-hàng-ngày)
3. [Quy Trình Tắt Hệ Thống](#3-quy-trình-tắt-hệ-thống)
4. [Checklist Giám Sát](#4-checklist-giám-sát)
5. [Xử Lý Sự Cố (Incident Response)](#5-xử-lý-sự-cố)
6. [Bảo Trì Định Kỳ](#6-bảo-trì-định-kỳ)
7. [Backup & Recovery](#7-backup--recovery)
8. [Monitoring Thresholds & Cảnh Báo](#8-monitoring-thresholds--cảnh-báo)
9. [Auto-Retrain Schedule](#9-auto-retrain-schedule)
10. [Log Reference](#10-log-reference)
11. [Escalation Matrix](#11-escalation-matrix)

---

## 1. Kiến Trúc Hệ Thống

```
┌──────────────────────────────────────────────────────────┐
│  PROCESS 1: MT5 Terminal (GUI)                           │
│  ┌─────────────────────┐  ┌─────────────────────────┐   │
│  │ XauDayTrader.mq5    │  │ ATS_Panel.mq5           │   │
│  │ (Expert Advisor)    │  │ (Indicator — top-left)  │   │
│  │ Nhận ZMQ → OrderSend│  │ Đọc live_state.json     │   │
│  │ Ghi ats_chart_*.txt │  │ Hiển thị 7 sections     │   │
│  └─────────────────────┘  └─────────────────────────┘   │
└──────────────────┬───────────────────────────────────────┘
                   │ ZMQ tcp://127.0.0.1:5555
                   │ File: ats_chart_*.txt (symbol detection)
┌──────────────────▼───────────────────────────────────────┐
│  PROCESS 2: python main.py multi-live                    │
│  - MultiRunner: scan ats_chart_*.txt mỗi 5s             │
│  - Mỗi symbol: LiveTickStream → Features → PPO → Signal  │
│  - AutoRetrainer: weekly (Mon) + drift detection         │
│  - Ghi live_state.json mỗi ~10s (heartbeat)             │
│  - Ghi worker_status.json mỗi khi status thay đổi       │
└──────────────────┬───────────────────────────────────────┘
                   │ đọc live_state.json (mỗi 5s)
┌──────────────────▼───────────────────────────────────────┐
│  PROCESS 3: streamlit run dashboard/app.py               │
│  - Dashboard web localhost:8501                          │
│  - Refresh mỗi 5 giây                                   │
└──────────────────┬───────────────────────────────────────┘
                   │ HTTP alerts
┌──────────────────▼───────────────────────────────────────┐
│  PROCESS 4: Telegram Bot (chạy trong Process 2)          │
│  - Cảnh báo: kill switch, drawdown, lệnh mở/đóng        │
└──────────────────────────────────────────────────────────┘
```

**Phụ thuộc khởi động:** MT5 phải chạy trước multi-runner (cần kết nối MT5 để lấy tick).

### File Quan Trọng

| File | Vị trí | Ghi bởi | Đọc bởi |
|---|---|---|---|
| `live_state.json` | `logs/` + MT5 Common Files | multi-runner (heartbeat ~10s) | Streamlit, Telegram, ATS_Panel |
| `worker_status.json` | `logs/` | multi-runner | Streamlit |
| `signal_server.log` | `logs/` | multi-runner | Operator, Streamlit |
| `ats_chart_*.txt` | MT5 Common Files | ATS_Panel Indicator | multi-runner |
| `ppo_*.zip` | `ai_models/checkpoints/` | train / AutoRetrainer | multi-runner |

### Cấu Trúc `live_state.json`

```json
{
  "XAUUSD": {
    "position": 1,
    "entry_price": 3200.50,
    "unrealized_pnl": 12.30,
    "regime": 0,
    "kelly_f": 0.0167,
    "drawdown_pct": 0.5,
    "last_signal": {},
    "timestamp": "2026-04-16T10:38:02Z",
    "model_version": "ppo_xauusd [2026-04-15 23:32]",
    "is_training": false,
    "last_retrain_time": "",
    "last_retrain_reason": "",
    "win_rate": 0.55,
    "total_trades": 34,
    "model_sharpe": 1.23,
    "signals_history": [
      {"s": "BUY", "p": 3200.50, "w": 0.57, "l": 0.10, "r": 1.8, "t": "08:32"}
    ]
  },
  "_system": {
    "kill_active": false,
    "kill_reason": "",
    "total_signals": 34,
    "last_heartbeat": "2026-04-16T10:38:02Z"
  },
  "_account": {
    "equity": 10120.0,
    "balance": 10000.0,
    "drawdown_pct": 0.5
  }
}
```

---

## 2. Quy Trình Khởi Động Hàng Ngày

### Thời Điểm Khuyến Nghị

Khởi động trước **7:30 GMT** (30 phút trước session London mở):
- Đủ thời gian phát hiện lỗi trước khi thị trường active
- Worker load model < 5 giây (đã có model từ trước)

### SOP Khởi Động

```
☐ 1.  Kiểm tra kết nối internet ổn định
☐ 2.  Mở MT5 → đăng nhập tài khoản (demo/live)
☐ 3.  Xác nhận "Connected" ở góc phải dưới MT5
☐ 4.  Terminal 1:
        cd d:\xau_ats && venv\Scripts\activate && python main.py multi-live
☐ 5.  Xác nhận log: "Multi-runner started. Scanning..."
☐ 6.  Terminal 2:
        cd d:\xau_ats && venv\Scripts\activate && streamlit run dashboard/app.py
☐ 7.  Mở http://localhost:8501 — xác nhận dashboard load
☐ 8.  Trong MT5: với mỗi chart symbol cần giao dịch:
        a. Gắn XauDayTrader EA (Navigator → Expert Advisors)
        b. Gắn ATS_Panel Indicator (Navigator → Indicators)
           → Allow algo trading ✅, Allow DLL imports ✅
☐ 9.  Xác nhận dashboard: "waiting" → "live" (lần đầu: "training" → 3-5 phút)
☐ 10. Xác nhận ATS_Panel hiện model_version và heartbeat < 15s
☐ 11. Kiểm tra Telegram: nhận thông báo "System started" không?
```

### Dấu Hiệu Khởi Động Thành Công

| Indicator | Giá trị kỳ vọng |
|---|---|
| Dashboard status | 🟢 LIVE |
| Worker status XAUUSD | 🟢 live |
| Heartbeat (dashboard) | < 10 giây |
| ATS_Panel → AI MODEL | Version hiển thị, heartbeat < 15s |
| ATS_Panel → SYSTEM | Updated timestamp khớp giờ hiện tại |
| Log | Không có `ERROR` trong 1 phút đầu |

---

## 3. Quy Trình Tắt Hệ Thống

### Tắt Cuối Ngày (sau 22h GMT)

```
☐ 1. Xác nhận tất cả vị thế đã đóng trong MT5
      (Tab Trade → phải rỗng)
☐ 2. Terminal 1: Ctrl+C
      Chờ: "All workers stopped."
☐ 3. Terminal 2 (Streamlit): Ctrl+C
☐ 4. MT5: xóa ATS_Panel Indicator và XauDayTrader EA khỏi các chart
      (giúp tránh EA tự khởi động lại khi MT5 restart)
☐ 5. Tắt MT5 (tùy chọn)
```

### Tắt Khẩn Cấp

```bash
# Dừng toàn bộ Python processes ngay lập tức
taskkill /f /im python.exe

# Kiểm tra lệnh còn mở trong MT5 → đóng thủ công nếu cần
```

**Sau tắt khẩn cấp:** Luôn kiểm tra MT5 — lệnh đang mở không tự đóng khi Python tắt.

---

## 4. Checklist Giám Sát

### Giám Sát Tự Động (Telegram)

Không cần làm gì, hệ thống tự cảnh báo khi:
- Kill switch kích hoạt (drawdown > 15%)
- Drawdown > 5%
- Heartbeat mất > 45 giây
- Lệnh mở/đóng

### Kiểm Tra 4 Lần/Ngày

| Thời điểm | Kiểm tra |
|---|---|
| 08:00 GMT (London open) | Dashboard 🟢, heartbeat OK, không có ERROR log |
| 13:00 GMT (NY open) | Worker status tất cả live, drawdown < 5% |
| 17:00 GMT (peak volume) | Equity, P&L, bất thường nào không |
| 21:30 GMT (trước EOD close) | Tất cả vị thế sẽ tự đóng lúc 22h GMT |

### Dashboard Checklist

```
☐ Status badge = 🟢 LIVE (không phải 🔴 KILLED)
☐ Heartbeat < 15 giây
☐ Drawdown < 10% (cảnh báo), < 15% (critical → kill switch)
☐ Mọi symbol active đều ở "live" (không phải "error")
☐ Equity curve không có spike bất thường
```

### ATS_Panel Checklist (MT5)

```
☐ SYSTEM → Heartbeat < 15s
☐ SYSTEM → Updated = giờ hiện tại
☐ AI MODEL → Status = Running (không phải trống)
☐ AI MODEL → Version có tên model (không phải "---")
☐ POSITION → Status khớp với vị thế thực trong MT5
```

---

## 5. Xử Lý Sự Cố

### 5.1 Kill Switch Kích Hoạt

**Triệu chứng:** Dashboard 🔴 KILLED, Telegram cảnh báo, không có lệnh mới, ATS_Panel SYSTEM → Kill hiển thị lý do.

**Đây là tính năng bảo vệ — không phải lỗi.**

**Quy trình:**
```
1. Xem dashboard tab System → "Kill reason"
   Hoặc: ATS_Panel → SYSTEM → Kill
2. Xác nhận tất cả lệnh đã đóng trong MT5
3. Phân tích nguyên nhân drawdown:
   - Sự kiện bất thường (news, flash crash)?
   - Model performance kém dần?
   - Lỗi kỹ thuật (slippage, wrong lot)?
4. Nếu news/event 1 lần: chờ thị trường ổn định → reset
5. Nếu model performance kém: retrain trước khi reset
6. Reset và restart:
   python main.py reset-kill
   → Gắn lại EA và Indicator vào chart
```

**Không bao giờ reset kill switch mà không phân tích nguyên nhân.**

---

### 5.2 Worker Lỗi (🔴 error)

**Triệu chứng:** Dashboard hiện 🔴 error cho một symbol cụ thể.

**Tìm lỗi:**
```bash
Select-String -Path d:\xau_ats\logs\signal_server.log -Pattern "ERROR|exception|crashed"
```

**Bảng phân loại lỗi:**

| Lỗi trong log | Nguyên nhân | Giải pháp |
|---|---|---|
| `MT5 init failed` | MT5 chưa chạy hoặc disconnect | Kiểm tra MT5, restart |
| `Insufficient memory` | RAM đầy | Đóng bớt programs, giảm `AUTO_TRAIN_BARS` |
| `ZMQ Address in use` | Port 5555 bị chiếm bởi process cũ | `taskkill /f /im python.exe` → restart |
| `No module named 'X'` | Thư viện chưa install | `pip install -r requirements.txt` |
| `NaN in observation` | Feature pipeline lỗi với dữ liệu xấu | Tự heal sau 3 tick |
| `load_ppo failed` | File model bị corrupt | Xóa file zip → để auto-retrain |

**Restart worker cụ thể:**
1. MT5: xóa ATS_Panel Indicator khỏi chart symbol lỗi → gắn lại
2. Multi-runner tự detect → spawn worker mới

---

### 5.3 ATS_Panel Hiển Thị Trống (MODEL/SIGNALS rỗng)

**Triệu chứng:** Section AI MODEL hoặc SIGNALS không có dữ liệu.

**Kiểm tra:**
```bash
# 1. Backend có đang chạy không?
tasklist | findstr python

# 2. Worker status
type d:\xau_ats\logs\worker_status.json

# 3. JSON có model_version không?
python -c "
import json
from pathlib import Path
p = Path('C:/Users/%USERNAME%/AppData/Roaming/MetaQuotes/Terminal/Common/Files/ats_live_state.json')
d = json.loads(p.read_text(encoding='utf-8'))
for sym, s in d.items():
    if sym.startswith('_'): continue
    print(sym, ':', s.get('model_version','MISSING'))
"
```

**Giải pháp:**
- Nếu không có Python → `python main.py multi-live`
- Nếu đang chạy nhưng `model_version=MISSING` → chờ 15 giây (1 heartbeat)
- Nếu vẫn MISSING → xem log xem heartbeat thread có lỗi không

---

### 5.4 MT5 Mất Kết Nối

**Triệu chứng:** MT5 hiện "Connection lost".

```
1. Kiểm tra internet
2. MT5 thường tự reconnect sau 30–60 giây
3. Nếu không tự reconnect: MT5 → File → Login → đăng nhập lại
4. Worker Python tự tiếp tục sau khi MT5 kết nối lại
```

**Lệnh đang mở:** Vẫn tồn tại trên server broker, không bị ảnh hưởng.

---

### 5.5 Dashboard Không Load

**Triệu chứng:** `http://localhost:8501` không mở được.

```bash
# Kiểm tra
tasklist | findstr streamlit

# Restart
streamlit run d:\xau_ats\dashboard\app.py
```

Dashboard down không ảnh hưởng giao dịch — multi-runner chạy độc lập.

---

### 5.6 Không Nhận Được Telegram

```
1. Kiểm tra TELEGRAM_TOKEN và TELEGRAM_CHAT_ID trong .env
2. Nhắn /start cho bot để unblock
3. Kiểm tra log: Select-String signal_server.log -Pattern "telegram"
4. Fallback: đọc dashboard và log trực tiếp
```

---

### 5.7 Lệnh Sai (Wrong Lot / Wrong Direction)

**Ngay lập tức:**
```
1. MT5: đóng lệnh sai thủ công (Right click → Close)
2. Dừng Python: Ctrl+C
3. KHÔNG restart cho đến khi phân tích xong
```

**Phân tích:**
```bash
# Xem signal lúc đó
Select-String -Path d:\xau_ats\logs\signal_server.log -Pattern "Published"

# Xem live state
type d:\xau_ats\logs\live_state.json
```

**Nguyên nhân thường gặp:** Lot quá cao (Kelly estimate sai), wrong side (model glitch khi news). Retrain nếu model có vấn đề (xem mục 9).

---

## 6. Bảo Trì Định Kỳ

### Hàng Ngày

```
☐ Xem equity curve trên dashboard — không có spike bất thường
☐ Xem log tail: Get-Content logs\signal_server.log -Tail 20
☐ Xác nhận không có "CRITICAL" hoặc "ERROR" trong log
☐ ATS_Panel: Model Status = Running, heartbeat cập nhật
```

### Hàng Tuần (Thứ Hai sáng)

```
☐ Xem win rate tuần qua:
    python main.py stats --symbol XAUUSD --days 7

☐ Kiểm tra xem auto-retrain thứ Hai đã chạy chưa:
    Select-String logs\signal_server.log -Pattern "AutoRetrain|weekly_schedule"

☐ Nếu win rate < 43% và auto-retrain chưa chạy → retrain thủ công:
    python main.py train --symbol XAUUSD --bars 50000

☐ Rotate logs (giữ 30 ngày):
    python main.py rotate-logs

☐ Kiểm tra disk space:
    dir d:\xau_ats\logs\
    (logs không nên > 500 MB)

☐ Kiểm tra model backups:
    dir d:\xau_ats\ai_models\checkpoints\
    (xóa backups > 4 tuần tuổi)

☐ Restart hệ thống để clear memory leaks tiềm ẩn
```

### Hàng Tháng

```
☐ Chạy walk-forward backtest:
    python main.py backtest --symbol XAUUSD --months 1
    (kỳ vọng: Sharpe > 0.5, win rate > 45%)

☐ Review drawdown và win rate tháng qua trên dashboard

☐ Kiểm tra requirements cần update không:
    pip list --outdated
    (upgrade cẩn thận — test sau khi upgrade)

☐ Backup models và config (xem mục 7)

☐ Review config.py: có cần điều chỉnh KELLY_FRACTION hay MAX_DRAWDOWN_PCT không?
```

### Hàng Quý

```
☐ Review tổng thể chiến lược: tham số có cần thay đổi không?
☐ Kiểm tra broker conditions (spread, commission, leverage thay đổi không)
☐ Update MT5 và Python nếu cần
☐ Test trên demo account trước khi thay đổi tham số quan trọng
```

---

## 7. Backup & Recovery

### Dữ Liệu Cần Backup

| File/Thư mục | Tần suất | Giữ bao lâu | Ghi chú |
|---|---|---|---|
| `ai_models/checkpoints/*.zip` | Hàng tuần | 4 phiên bản gần nhất | Model đã train — quan trọng nhất |
| `logs/*.log` | Hàng ngày | 30 ngày | Audit trail |
| `config.py` | Khi thay đổi | Vĩnh viễn | Cấu hình quan trọng |
| `.env` | Khi thay đổi | Vĩnh viễn | Credentials — lưu nơi an toàn |
| `data/*.parquet` | Hàng tuần | 60 ngày | Cache dữ liệu |
| `logs/live_state.json` | Không cần | — | Tự regenerate |

### Script Backup Tự Động

Tạo file `backup.bat`:

```batch
@echo off
set BACKUP_DIR=d:\xau_ats_backup\%date:~10,4%-%date:~4,2%-%date:~7,2%
mkdir "%BACKUP_DIR%"

:: Backup models (bao gồm cả backup_*.zip từ auto-retrain)
xcopy /s /y "d:\xau_ats\ai_models\checkpoints\*.zip" "%BACKUP_DIR%\models\"

:: Backup config
copy "d:\xau_ats\config.py" "%BACKUP_DIR%\"

:: Backup last 7 days of logs
forfiles /p "d:\xau_ats\logs" /s /m *.log /d -7 /c "cmd /c copy @path %BACKUP_DIR%\logs\"

echo Backup completed to %BACKUP_DIR%
```

Thêm vào Windows Task Scheduler: chạy `backup.bat` lúc 23:30 GMT hàng ngày.

### Recovery

**Khôi phục model:**
```bash
copy d:\xau_ats_backup\YYYY-MM-DD\models\ppo_xauusd.zip d:\xau_ats\ai_models\checkpoints\
```

**Rollback về model trước khi auto-retrain:**
```bash
# Auto-retrain tự backup vào checkpoints/ với tên ppo_xauusd_backup_YYYYMMDD_HHMM.zip
dir d:\xau_ats\ai_models\checkpoints\*backup*

# Rollback
copy d:\xau_ats\ai_models\checkpoints\ppo_xauusd_backup_20260415_2332.zip ^
     d:\xau_ats\ai_models\checkpoints\ppo_xauusd.zip
# → Restart multi-runner để load model cũ
```

**Khôi phục toàn bộ:**
```bash
taskkill /f /im python.exe
xcopy /s /y d:\xau_ats_backup\YYYY-MM-DD\ d:\xau_ats\
cd d:\xau_ats && venv\Scripts\activate && pip install -r requirements.txt
python main.py multi-live
```

---

## 8. Monitoring Thresholds & Cảnh Báo

### Ngưỡng Cảnh Báo

| Metric | Warning | Critical | Action |
|---|---|---|---|
| **Drawdown** | 5% | 15% (auto kill) | Warning: giám sát chặt hơn |
| **Heartbeat age** | 15 giây | 45 giây (Telegram alert) | Kiểm tra backend |
| **Win rate (50 lệnh)** | 45% | 43% (auto retrain) | Monitor, xem ATS_Panel AI MODEL |
| **Sharpe (rolling)** | 0.5 | 0.0 | Cân nhắc retrain thủ công |
| **Slippage trung bình** | 5 pips | 15 pips | Kiểm tra spread broker |
| **Worker errors** | 1 lỗi | 3 lỗi/giờ | Xem log, restart worker |
| **Log file size** | 200 MB | 500 MB | Rotate logs |
| **RAM sử dụng** | 70% | 85% | Giảm số symbols |

### Telegram Alert Mapping

| Alert | Trigger | Hành động |
|---|---|---|
| `⚠️ Kill switch activated` | Drawdown > 15% | Xem nguyên nhân, phân tích log |
| `⚠️ Heartbeat lost` | HB > 45s | Kiểm tra Python còn chạy không |
| `📊 High drawdown: X%` | Drawdown > 5% | Giám sát chặt hơn |
| `🔄 Auto-retrain started` | Weekly/drift trigger | Theo dõi, xem ATS_Panel Status |
| `✅ Model updated` | New model accepted | Xác nhận Version mới trên panel |
| `⚠️ Model rejected` | New model worse | Bình thường — model cũ vẫn hoạt động |
| `📈 Long opened` | Position = 1 | Xác nhận trong MT5 |
| `📉 Short opened` | Position = -1 | Xác nhận trong MT5 |
| `✅ Position closed` | Position → 0 | Xem P&L của lệnh |

---

## 9. Auto-Retrain Schedule

### Cơ Chế Tự Động

Hệ thống có **2 trigger retrain tự động**, không cần can thiệp:

| Trigger | Điều kiện | Tần suất tối đa |
|---|---|---|
| **Weekly schedule** | Mỗi thứ Hai, một lần/tuần ISO | 1 lần/tuần |
| **Drift detection** | Win rate < 43% (min 30 lệnh) | 1 lần/24h |

### Quá Trình Auto-Retrain

```
1. AutoRetrainer phát hiện trigger
2. Training ngầm — model mới lưu vào ppo_SYMBOL_candidate.zip
3. Live trading TIẾP TỤC với model hiện tại trong lúc training
4. Đánh giá: 3 time windows × 2 seeds → mean Sharpe
5. Nếu new_sharpe >= old_sharpe × 0.90:
     - Backup model cũ: ppo_SYMBOL_backup_YYYYMMDD_HHMM.zip
     - Promote candidate → ppo_SYMBOL.zip
     - Swap ngay không gián đoạn giao dịch
     - Reset PerformanceMonitor cho window mới
6. Nếu model mới kém hơn:
     - Xóa candidate file
     - Giữ nguyên model hiện tại
```

### Theo Dõi Auto-Retrain

**ATS_Panel (MT5):**
```
AI MODEL → Status: TRAINING    ← đang train
AI MODEL → Status: Running     ← đã xong
AI MODEL → Version: ppo_xauusd [2026-04-21 08:00]  ← version mới sau khi accept
AI MODEL → Last Train: 2026-04-21T08:00Z
AI MODEL → Reason: weekly_schedule (ISO 2026-W17)
```

**Log:**
```bash
# Xem tất cả retrain events
Select-String -Path d:\xau_ats\logs\signal_server.log -Pattern "AutoRetrain|Retrain"

# Xem kết quả accept/reject
Select-String -Path d:\xau_ats\logs\signal_server.log -Pattern "ACCEPTED|REJECTED"
```

### Retrain Thủ Công

Khi cần retrain ngay (không chờ lịch tự động):

```bash
# Bước 1: Dừng worker — xóa ATS_Panel khỏi chart trong MT5

# Bước 2: Train với dữ liệu mới
python main.py train --symbol XAUUSD --bars 50000
# Chờ ~3–5 phút

# Bước 3: Backtest nhanh để xác nhận
python main.py backtest --symbol XAUUSD --months 1
# Kỳ vọng: Sharpe > 0.5, win rate > 45%

# Bước 4: Gắn lại ATS_Panel vào chart → worker load model mới
```

### Khi Nào Nên Retrain Thủ Công?

| Tình huống | Hành động |
|---|---|
| Sự kiện thị trường lớn (Fed, crisis) | Retrain thủ công ngay sau sự kiện |
| Auto-retrain reject liên tục 3 lần | Retrain thủ công + xem log lý do |
| Model bị corrupt (lỗi load) | Xóa file + retrain thủ công |
| Muốn retrain trước thứ Hai | Retrain thủ công bất kỳ lúc nào |

---

## 10. Log Reference

### Vị Trí Log

| Log | Vị trí | Nội dung |
|---|---|---|
| Giao dịch & tín hiệu | `d:\xau_ats\logs\signal_server.log` | Signal publish, heartbeat, retrain |
| Multi-runner | `d:\xau_ats\logs\multi_runner.log` | Chart detection, worker start/stop |
| Trạng thái workers | `d:\xau_ats\logs\worker_status.json` | JSON: symbol → status |
| Trạng thái live | `d:\xau_ats\logs\live_state.json` | JSON đầy đủ: equity, positions, model |
| MT5 Journal | MT5 → View → Terminal → Journal | EA activity, order events |
| MT5 Experts | MT5 → View → Terminal → Experts | EA errors, ZMQ messages |

### Đọc Log Hiệu Quả (PowerShell)

```powershell
# 50 dòng cuối
Get-Content d:\xau_ats\logs\signal_server.log -Tail 50

# Chỉ lỗi
Select-String -Path d:\xau_ats\logs\signal_server.log -Pattern "ERROR|CRITICAL"

# Tín hiệu giao dịch
Select-String -Path d:\xau_ats\logs\signal_server.log -Pattern "Published"

# Retrain events
Select-String -Path d:\xau_ats\logs\signal_server.log -Pattern "AutoRetrain|Retrain|ACCEPTED|REJECTED"

# Theo symbol
Select-String -Path d:\xau_ats\logs\signal_server.log -Pattern "\[XAUUSD\]"

# Theo ngày hôm nay
$today = Get-Date -Format "yyyy-MM-dd"
Select-String -Path d:\xau_ats\logs\signal_server.log -Pattern $today

# Theo dõi liên tục
Get-Content d:\xau_ats\logs\signal_server.log -Wait -Tail 50
```

### Log Level

| Level | Ý nghĩa | Hành động |
|---|---|---|
| `INFO` | Hoạt động bình thường | Không cần |
| `WARNING` | Bất thường nhẹ (retry, fallback) | Theo dõi |
| `ERROR` | Lỗi đã xử lý nhưng cần điều tra | Điều tra |
| `CRITICAL` | Lỗi nghiêm trọng, cần can thiệp ngay | Xử lý ngay |

### Ví Dụ Log Bình Thường

```
2026-04-16 08:01:23 INFO [XAUUSD] #47 Published: side=1 price=3200.50 lot=0.10 regime=0
2026-04-16 08:01:33 INFO [XAUUSD] Heartbeat: signal_count=47 model=ppo_xauusd [2026-04-15 23:32]
2026-04-16 09:00:01 INFO [XAUUSD] AutoRetrain START — reason: drift_detected (win_rate=41.2% < 43.0%)
2026-04-16 09:04:55 INFO [XAUUSD] ✓ New model ACCEPTED — Sharpe 1.23 → 1.41
```

### Ví Dụ Log Cần Chú Ý

```
2026-04-16 13:02:11 WARNING [XAUUSD] MT5 data fetch failed: timeout — using cache
2026-04-16 13:02:45 ERROR   [EURUSD] Worker crashed: ZMQError Address in use
2026-04-16 14:30:00 CRITICAL Kill switch activated: drawdown=16.2% > 15.0%
2026-04-16 15:00:01 INFO    [XAUUSD] ✗ New model REJECTED — new=0.82 < threshold=1.11
```

---

## 11. Escalation Matrix

### Mức Độ Sự Cố

| Mức | Mô tả | Ví dụ | Thời gian xử lý |
|---|---|---|---|
| **P1 — Khẩn Cấp** | Tổn thất tài chính đang xảy ra | Lệnh sai kích thước, drawdown tăng nhanh | < 5 phút |
| **P2 — Cao** | Hệ thống dừng hoàn toàn | Kill switch, tất cả workers error | < 30 phút |
| **P3 — Trung Bình** | Partial outage | 1 symbol lỗi, dashboard down | < 2 giờ |
| **P4 — Thấp** | Cảnh báo hiệu suất | Win rate thấp, model reject liên tục | Ngày hôm sau |

### P1 — Xử Lý Khẩn Cấp

```
Phát hiện → Dừng giao dịch ngay → Đánh giá tổn thất → Bảo toàn vốn → Phân tích
```

**Bước 1 (< 1 phút):**
```bash
taskkill /f /im python.exe
```

**Bước 2 (< 2 phút):**
- MT5 → kiểm tra tất cả lệnh đang mở
- Đóng thủ công những lệnh nguy hiểm

**Bước 3 (< 5 phút):**
- Ghi lại: equity hiện tại, lệnh đang mở, log lúc xảy ra
- Screenshot dashboard và MT5

**Bước 4 (sau khi ổn định):**
- Phân tích log tìm nguyên nhân
- Quyết định: sửa và restart, hay chờ phân tích kỹ

---

### P2 — Kill Switch Activated

```
1. Đừng panic — đây là tính năng bảo vệ hoạt động đúng
2. Dashboard → System tab → Kill reason
   Hoặc: ATS_Panel → SYSTEM → Kill
3. Select-String signal_server.log -Pattern "Kill switch"
4. Xác nhận tất cả lệnh đã đóng trong MT5
5. Phân tích:
   - News event 1 lần → reset sau khi thị trường ổn định
   - Win rate kém dài hạn → retrain trước khi reset
6. python main.py reset-kill → restart
```

### Quyết Định Reset vs Không Reset

| Tình huống | Quyết định | Lý do |
|---|---|---|
| Drawdown do Fed statement bất ngờ | Reset sau 2–4 giờ | Market freak event, model vẫn OK |
| Drawdown do chuỗi 10 lệnh thua liên tiếp | Retrain trước | Model có vấn đề |
| Drawdown do lot quá to | Điều chỉnh `KELLY_FRACTION` | Config issue |
| Drawdown không rõ nguyên nhân | Phân tích kỹ 24h | Không reset vội |

---

## Phụ Lục A: Health Check Commands

```powershell
# Kiểm tra Python processes đang chạy
Get-Process python -ErrorAction SilentlyContinue | Select-Object Id, CPU, WorkingSet

# Kiểm tra port 5555 (ZMQ)
netstat -ano | findstr 5555

# Kiểm tra disk space
Get-PSDrive D | Select-Object Used, Free

# Kiểm tra RAM
Get-CimInstance Win32_OperatingSystem | Select-Object TotalVisibleMemorySize, FreePhysicalMemory

# Xem live state nhanh
python -c "
import json
from pathlib import Path
d = json.loads(Path('d:/xau_ats/logs/live_state.json').read_text())
sys = d.get('_system', {})
acc = d.get('_account', {})
print(f'Equity: {acc.get(\"equity\", 0):.2f}  Kill: {sys.get(\"kill_active\", False)}  HB: {sys.get(\"last_heartbeat\",\"?\")[11:19]}')
for sym, s in d.items():
    if sym.startswith('_'): continue
    print(f'  {sym}: pos={s.get(\"position\")} model={s.get(\"model_version\",\"MISSING\")[:30]} wr={s.get(\"win_rate\",0):.0%}')
"

# Xem worker status
type d:\xau_ats\logs\worker_status.json
```

---

## Phụ Lục B: Windows Task Scheduler — Auto-Start

Để hệ thống tự khởi động Python sau reboot:

1. Mở Task Scheduler → Create Basic Task
2. Trigger: "When the computer starts" + delay 60s
3. Action: Start a Program
   - Program: `d:\xau_ats\venv\Scripts\python.exe`
   - Arguments: `d:\xau_ats\main.py multi-live`
   - Start in: `d:\xau_ats`

**Lưu ý:** Vẫn cần mở MT5 thủ công và gắn lại XauDayTrader EA + ATS_Panel Indicator — MT5 yêu cầu đăng nhập tương tác.

---

## Phụ Lục C: Tài Nguyên

| Tài nguyên | Mô tả |
|---|---|
| `docs/user_guide.md` | Hướng dẫn sử dụng cho người dùng cuối |
| `docs/technical_guide.md` | Tài liệu kỹ thuật chi tiết (thuật toán, model) |
| `tests/test_ats.py` | Test suite — chạy trước mỗi deployment |
| `config.py` | Tất cả tham số có chú thích |
| MT5 Help → MQL5 Reference | Tài liệu MQL5 chính thức |
