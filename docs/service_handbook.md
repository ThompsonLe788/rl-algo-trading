# Sổ Tay Vận Hành: ATS XAU/USD

> **Dành cho ai?** Người chịu trách nhiệm vận hành hệ thống hàng ngày — bao gồm khởi động/tắt theo lịch, giám sát hiệu suất, ứng phó sự cố, và bảo trì định kỳ.

---

## Mục Lục

1. [Kiến Trúc Hệ Thống — Tóm Tắt Vận Hành](#1-kiến-trúc-hệ-thống--tóm-tắt-vận-hành)
2. [Quy Trình Khởi Động Hàng Ngày](#2-quy-trình-khởi-động-hàng-ngày)
3. [Quy Trình Tắt Hệ Thống](#3-quy-trình-tắt-hệ-thống)
4. [Checklist Giám Sát](#4-checklist-giám-sát)
5. [Xử Lý Sự Cố (Incident Response)](#5-xử-lý-sự-cố)
6. [Bảo Trì Định Kỳ](#6-bảo-trì-định-kỳ)
7. [Backup & Recovery](#7-backup--recovery)
8. [Monitoring Thresholds & Cảnh Báo](#8-monitoring-thresholds--cảnh-báo)
9. [Retrain Schedule](#9-retrain-schedule)
10. [Log Reference](#10-log-reference)
11. [Escalation Matrix](#11-escalation-matrix)

---

## 1. Kiến Trúc Hệ Thống — Tóm Tắt Vận Hành

```
┌─────────────────────────────────────────────────────────┐
│  PROCESS 1: MT5 Terminal (GUI)                          │
│  - Kết nối broker, nhận tick                            │
│  - EA ATS_Panel chạy trong terminal                     │
│  - Nhận ZMQ signal → OrderSend()                        │
└─────────────────┬───────────────────────────────────────┘
                  │ ZMQ tcp://127.0.0.1:5555
┌─────────────────▼───────────────────────────────────────┐
│  PROCESS 2: python main.py multi-live                   │
│  - Quản lý 1 thread/symbol                             │
│  - Mỗi thread: LiveTickStream → Features → PPO → Signal │
│  - Ghi live_state.json mỗi ~2 giây                     │
└─────────────────┬───────────────────────────────────────┘
                  │ đọc live_state.json
┌─────────────────▼───────────────────────────────────────┐
│  PROCESS 3: streamlit run dashboard/app.py              │
│  - Dashboard web localhost:8501                         │
│  - Refresh mỗi 5 giây                                  │
└─────────────────────────────────────────────────────────┘
                  │ HTTP alerts
┌─────────────────▼───────────────────────────────────────┐
│  PROCESS 4: telegram_bot (chạy trong Process 2)         │
│  - Cảnh báo tự động: kill switch, drawdown, lệnh        │
└─────────────────────────────────────────────────────────┘
```

**Phụ thuộc khởi động:** MT5 phải chạy trước khi multi-runner, vì multi-runner cần kết nối MT5.

**File quan trọng:**
| File | Vị trí | Cập nhật bởi | Đọc bởi |
|---|---|---|---|
| `live_state.json` | `logs/` + MT5 Common Files | multi-runner | Streamlit, Telegram, ATS_StrategyView |
| `worker_status.json` | `logs/` | multi-runner | Streamlit |
| `signal_server.log` | `logs/` | multi-runner | Operator, Streamlit |
| `ats_chart_*.txt` | MT5 Common Files | ATS_Panel EA | multi-runner |
| `ppo_*.zip` | `ai_models/checkpoints/` | train command | multi-runner |

---

## 2. Quy Trình Khởi Động Hàng Ngày

### Thời Điểm Khuyến Nghị
Khởi động trước **7:30 GMT** (30 phút trước session London mở) để:
- Đủ thời gian phát hiện lỗi trước khi thị trường active
- Worker kịp load model (< 5 giây nếu đã train)

### SOP Khởi Động (Standard Operating Procedure)

```
☐ 1. Kiểm tra kết nối internet ổn định
☐ 2. Mở MT5 → đăng nhập tài khoản (demo/live)
☐ 3. Xác nhận thanh trạng thái MT5 hiện "Connected" (góc phải dưới)
☐ 4. Mở Terminal 1:
     cd d:\xau_ats && venv\Scripts\activate && python main.py multi-live
☐ 5. Xác nhận log: "Multi-runner started. Scanning..."
☐ 6. Mở Terminal 2:
     cd d:\xau_ats && venv\Scripts\activate && streamlit run dashboard/app.py
☐ 7. Mở http://localhost:8501 — xác nhận dashboard load
☐ 8. Trong MT5: gắn ATS_Panel EA vào từng chart symbol
     (XAUUSD bắt buộc, các symbol khác tùy)
☐ 9. Xác nhận dashboard: trạng thái chuyển từ "waiting" → "live"
     (lần đầu sẽ là "training" → chờ 3-5 phút)
☐ 10. Kiểm tra Telegram: bot gửi thông báo "System started" không?
```

### Dấu Hiệu Khởi Động Thành Công

| Indicator | Giá trị kỳ vọng |
|---|---|
| Dashboard status | 🟢 LIVE |
| Worker status XAUUSD | 🟢 live |
| Heartbeat | < 10 giây |
| Telegram | Nhận thông báo khởi động |
| Log | Không có `ERROR` trong 1 phút đầu |

---

## 3. Quy Trình Tắt Hệ Thống

### Tắt Cuối Ngày (sau 22h GMT — lệnh đã tự đóng)

```
☐ 1. Xác nhận tất cả vị thế đã đóng trong MT5
     (Orders tab → phải rỗng)
☐ 2. Trong Terminal 1: Ctrl+C
     Chờ: "All workers stopped."
☐ 3. Trong Terminal 2 (Streamlit): Ctrl+C
☐ 4. Trong MT5: xóa EA khỏi tất cả chart (tùy chọn — giúp tránh EA khởi động lại khi MT5 restart)
☐ 5. Tắt MT5 (tùy chọn)
```

### Tắt Khẩn Cấp

Khi cần dừng ngay lập tức (ví dụ: sự cố bất thường, drawdown lớn):

```bash
# Dừng toàn bộ Python processes
taskkill /f /im python.exe

# Kiểm tra lệnh còn mở trong MT5
# → Đóng thủ công nếu cần
```

**Sau khi tắt khẩn cấp:** Luôn kiểm tra MT5 — lệnh đang mở không tự đóng khi Python tắt.

---

## 4. Checklist Giám Sát

### Giám Sát Liên Tục (Automated — Telegram)

Không cần làm gì, hệ thống tự cảnh báo khi:
- Kill switch kích hoạt
- Drawdown > 5%
- Heartbeat mất > 45 giây

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
☐ Mọi symbol active đều ở trạng thái "live" (không phải "error")
☐ Equity curve không có spike bất thường
```

---

## 5. Xử Lý Sự Cố

### 5.1 Kill Switch Kích Hoạt

**Triệu chứng:** Dashboard 🔴 KILLED, Telegram cảnh báo, không có lệnh mới.

**Đây là tính năng bảo vệ — không phải lỗi.** Kill switch kích hoạt khi drawdown > 15%.

**Quy trình:**
```
1. Xác nhận lý do: xem dashboard tab System → "Kill reason"
2. Xác nhận tất cả lệnh đã đóng trong MT5
3. Phân tích nguyên nhân drawdown:
   - Sự kiện bất thường (news, flash crash)?
   - Model performance kém dần?
   - Lỗi kỹ thuật (slippage, wrong lot)?
4. Nếu tin tức/sự kiện bất thường → chờ thị trường ổn định
5. Nếu model performance kém → retrain trước khi restart
6. Reset và restart:
   python main.py reset-kill
   → Gắn lại EA vào chart
```

**Không bao giờ reset kill switch mà không phân tích nguyên nhân.**

---

### 5.2 Worker Lỗi (🔴 error)

**Triệu chứng:** Dashboard hiện 🔴 error cho một symbol cụ thể.

**Tìm lỗi:**
```bash
# Xem log đầy đủ
type d:\xau_ats\logs\signal_server.log | findstr /i "error\|exception\|crashed"
```

**Bảng phân loại lỗi:**

| Lỗi trong log | Nguyên nhân | Giải pháp |
|---|---|---|
| `MT5 init failed` | MT5 chưa chạy hoặc đã disconnect | Kiểm tra MT5, restart |
| `Insufficient memory` | RAM đầy | Đóng bớt programs, hoặc giảm `AUTO_TRAIN_BARS` |
| `ZMQ Address in use` | Port 5555 bị chiếm bởi process cũ | `taskkill /f /fi "imagename eq python.exe"` → restart |
| `No module named 'X'` | Thư viện chưa install | `pip install -r requirements.txt` |
| `NaN in observation` | Feature pipeline lỗi với dữ liệu xấu | Tự heal trong 3 tick (FEAT_INTERVAL=3) |
| `load_ppo failed` | File model bị corrupt | Xóa file zip và để auto-retrain |

**Restart worker cụ thể:**
1. Trong MT5: xóa EA khỏi chart symbol lỗi → gắn lại
2. Multi-runner tự detect và spawn worker mới

---

### 5.3 MT5 Mất Kết Nối

**Triệu chứng:** MT5 hiện "Connection lost" hoặc "No connection".

```
1. Kiểm tra internet
2. MT5 thường tự reconnect sau 30-60 giây
3. Nếu không reconnect: MT5 → File → Login → đăng nhập lại
4. Worker Python sẽ tiếp tục sau khi MT5 kết nối lại
```

**Lệnh đang mở khi MT5 mất kết nối:** Lệnh vẫn tồn tại trên server broker, không bị ảnh hưởng bởi kết nối client.

---

### 5.4 Dashboard Không Load / Lỗi

**Triệu chứng:** `http://localhost:8501` không mở được.

```bash
# Kiểm tra Streamlit có đang chạy không
tasklist | findstr streamlit
tasklist | findstr python

# Khởi động lại Streamlit
streamlit run d:\xau_ats\dashboard\app.py
```

Dashboard down không ảnh hưởng đến giao dịch — multi-runner chạy độc lập.

---

### 5.5 Không Nhận Được Telegram

```
1. Kiểm tra token và chat ID trong config.py
2. Kiểm tra bot chưa bị block (nhắn /start cho bot)
3. Kiểm tra log: grep "telegram" trong signal_server.log
4. Fallback: đọc dashboard và log trực tiếp
```

---

### 5.6 Lệnh Sai (Wrong Lot / Wrong Direction)

**Ngay lập tức:**
```
1. Trong MT5: đóng lệnh sai thủ công (Right click → Close)
2. Dừng Python: Ctrl+C trong terminal
3. KHÔNG restart cho đến khi phân tích xong
```

**Phân tích:**
```bash
# Xem signal lúc đó
type d:\xau_ats\logs\signal_server.log | findstr "Published"
# Xem live state lúc đó
type d:\xau_ats\logs\live_state.json
```

**Nguyên nhân thường gặp:** Lot quá cao (Kelly estimate sai), wrong side (model glitch khi news). Xem phần 9 để retrain nếu model có vấn đề.

---

## 6. Bảo Trì Định Kỳ

### Hàng Ngày

```
☐ Xem equity curve trên dashboard — không có spike bất thường
☐ Xem log tail: type logs\signal_server.log (20 dòng cuối)
☐ Xác nhận không có "CRITICAL" hoặc "ERROR" trong log
```

### Hàng Tuần (Thứ Hai sáng)

```
☐ Xem win rate tuần qua:
  python main.py stats --symbol XAUUSD --days 7
  
☐ Nếu win rate < 45% → lên kế hoạch retrain (xem mục 9)

☐ Rotate logs (giữ 30 ngày):
  python main.py rotate-logs

☐ Kiểm tra disk space:
  dir d:\xau_ats\logs\
  (logs không nên > 500 MB)

☐ Kiểm tra model age:
  dir d:\xau_ats\ai_models\checkpoints\
  (khuyến nghị retrain nếu model > 30 ngày tuổi)

☐ Restart hệ thống để clear memory leaks tiềm ẩn
```

### Hàng Tháng

```
☐ Retrain tất cả models với dữ liệu mới nhất (xem mục 9)

☐ Chạy walk-forward backtest để đánh giá performance:
  python main.py backtest --symbol XAUUSD --months 1

☐ Cập nhật requirements nếu có security patches:
  pip list --outdated
  pip install -r requirements.txt --upgrade (cẩn thận: test sau khi upgrade)

☐ Backup models và logs (xem mục 7)

☐ Review drawdown và win rate tháng qua
```

### Hàng Quý

```
☐ Review tổng thể chiến lược: có cần thay đổi tham số không?
☐ Kiểm tra broker conditions thay đổi không (spread, commission)
☐ Update MT5 và thư viện Python nếu cần
☐ Test với tài khoản demo trước khi thay đổi tham số quan trọng
```

---

## 7. Backup & Recovery

### Dữ Liệu Cần Backup

| File/Thư mục | Tần suất | Giữ bao lâu | Ghi chú |
|---|---|---|---|
| `ai_models/checkpoints/*.zip` | Hàng tuần | Giữ 4 phiên bản gần nhất | Model đã train — quan trọng |
| `logs/*.log` | Hàng ngày | 30 ngày | Audit trail |
| `logs/live_state.json` | N/A | Không cần | Regenerate tự động |
| `config.py` | Khi thay đổi | Vĩnh viễn | Cấu hình quan trọng |
| `data/*.parquet` | Hàng tuần | 60 ngày | Cache dữ liệu |

### Script Backup Tự Động

Tạo file `backup.bat`:

```batch
@echo off
set BACKUP_DIR=d:\xau_ats_backup\%date:~10,4%-%date:~4,2%-%date:~7,2%
mkdir "%BACKUP_DIR%"

:: Backup models
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

**Khôi phục toàn bộ từ backup:**
```bash
# 1. Stop tất cả processes
taskkill /f /im python.exe

# 2. Copy backup về
xcopy /s /y d:\xau_ats_backup\YYYY-MM-DD\ d:\xau_ats\

# 3. Reinstall dependencies nếu cần
cd d:\xau_ats && venv\Scripts\activate && pip install -r requirements.txt

# 4. Restart bình thường
```

---

## 8. Monitoring Thresholds & Cảnh Báo

### Ngưỡng Cảnh Báo

| Metric | Warning | Critical | Action |
|---|---|---|---|
| **Drawdown** | 5% | 15% (auto kill) | Warning: giám sát chặt hơn |
| **Heartbeat age** | 15 giây | 45 giây (Telegram alert) | Kiểm tra backend |
| **Win rate (50 lệnh)** | 45% | 40% | Cân nhắc retrain |
| **Slippage trung bình** | 5 pips | 15 pips | Kiểm tra spread broker |
| **Worker errors** | 1 lỗi | 3 lỗi/giờ | Xem log, restart worker |
| **Log file size** | 200 MB | 500 MB | Rotate logs |
| **RAM sử dụng** | 70% | 85% | Giảm số symbols |

### Telegram Alert Mapping

| Alert | Trigger | Hành động ngay |
|---|---|---|
| `⚠️ Kill switch activated` | Drawdown > 15% | Xem nguyên nhân, phân tích log |
| `⚠️ Heartbeat lost` | HB > 45s | Kiểm tra Python còn chạy không |
| `📊 High drawdown: X%` | Drawdown > 5% | Giám sát chặt hơn |
| `📈 Long opened` | Position = 1 | Xác nhận trong MT5 |
| `📉 Short opened` | Position = -1 | Xác nhận trong MT5 |
| `✅ Position closed` | Position → 0 | Xem P&L của lệnh |

---

## 9. Retrain Schedule

### Khi Nào Cần Retrain?

**Bắt buộc retrain khi:**
- Win rate < 40% trong 50 lệnh liên tiếp
- Sharpe ratio âm trong 1 tuần
- Sau sự kiện thị trường lớn (Fed pivot, geopolitical crisis)
- Model file bị corrupt (lỗi load)

**Khuyến nghị retrain khi:**
- Model > 30 ngày tuổi
- Win rate giảm từ >50% xuống 45-50% trong 2 tuần
- Regime thị trường thay đổi rõ rệt (ví dụ: từ low-vol sang high-vol)

**Không cần retrain khi:**
- Drawdown do news event 1 lần (outlier)
- Win rate tạm thấp trong < 1 tuần

### Quy Trình Retrain

```bash
# Bước 1: Dừng worker đang chạy
# Trong MT5: xóa EA khỏi chart XAUUSD

# Bước 2: Train với dữ liệu mới nhất (50,000 bar M1)
python main.py train --symbol XAUUSD --bars 50000

# Bước 3: Chờ training hoàn tất (~3-5 phút)
# Log sẽ hiện: "Auto-train complete → checkpoints/ppo_xauusd.zip"

# Bước 4: Chạy backtest nhanh để xác nhận model OK
python main.py backtest --symbol XAUUSD --months 1
# Kỳ vọng: Sharpe > 0.5, drawdown < 20%, win rate > 45%

# Bước 5: Nếu backtest OK → gắn lại EA vào chart
# Worker load model mới tự động
```

### Giữ Lại Model Cũ

Trước khi retrain, backup model cũ:
```bash
copy d:\xau_ats\ai_models\checkpoints\ppo_xauusd.zip d:\xau_ats\ai_models\checkpoints\ppo_xauusd_backup_%date:~10,4%%date:~4,2%%date:~7,2%.zip
```

Nếu model mới kém hơn → khôi phục model cũ:
```bash
copy ppo_xauusd_backup_YYYYMMDD.zip ppo_xauusd.zip
```

---

## 10. Log Reference

### Vị Trí Log

| Log | Vị trí | Nội dung |
|---|---|---|
| Giao dịch & tín hiệu | `d:\xau_ats\logs\signal_server.log` | Signal publish, heartbeat, errors |
| Trạng thái workers | `d:\xau_ats\logs\worker_status.json` | JSON: symbol → status |
| Trạng thái live | `d:\xau_ats\logs\live_state.json` | JSON: equity, positions, regime |
| MT5 Journal | MT5 → View → Terminal → Journal tab | EA activity, order events |
| MT5 Experts | MT5 → View → Terminal → Experts tab | EA errors, print() output |

### Đọc Log Hiệu Quả

```bash
# 50 dòng cuối
Get-Content d:\xau_ats\logs\signal_server.log -Tail 50

# Chỉ lỗi
Select-String -Path d:\xau_ats\logs\signal_server.log -Pattern "ERROR|CRITICAL"

# Tín hiệu giao dịch
Select-String -Path d:\xau_ats\logs\signal_server.log -Pattern "Published"

# Theo symbol
Select-String -Path d:\xau_ats\logs\signal_server.log -Pattern "\[XAUUSD\]"

# Theo khoảng thời gian (ví dụ: ngày hôm nay)
$today = Get-Date -Format "yyyy-MM-dd"
Select-String -Path d:\xau_ats\logs\signal_server.log -Pattern $today
```

### Log Level Meanings

| Level | Ý nghĩa | Hành động |
|---|---|---|
| `INFO` | Hoạt động bình thường | Không cần |
| `WARNING` | Bất thường nhẹ (retry, fallback) | Theo dõi |
| `ERROR` | Lỗi cụ thể đã được xử lý | Điều tra nguyên nhân |
| `CRITICAL` | Lỗi nghiêm trọng cần can thiệp ngay | Xử lý ngay |

### Ví Dụ Log Bình Thường

```
2025-01-15 08:01:23 INFO [#47] XAUUSD Published: side=1 price=2658.50 lot=0.1 regime=0
2025-01-15 08:01:28 INFO Heartbeat sent: XAUUSD signal_count=47
2025-01-15 08:06:15 INFO [#48] XAUUSD Published: side=0 price=2661.20 lot=0 regime=0
```

### Ví Dụ Log Cần Chú Ý

```
2025-01-15 13:02:11 WARNING [XAUUSD] MT5 data fetch failed: timeout — using cache
2025-01-15 13:02:45 ERROR [EURUSD] Worker crashed: ZMQError Address in use
2025-01-15 14:30:00 CRITICAL Kill switch activated: drawdown=16.2% > 15.0%
```

---

## 11. Escalation Matrix

### Mức Độ Sự Cố

| Mức | Mô tả | Ví dụ | Thời gian xử lý |
|---|---|---|---|
| **P1 — Khẩn Cấp** | Tổn thất tài chính đang xảy ra | Lệnh sai kích thước, drawdown tăng nhanh | Xử lý ngay (< 5 phút) |
| **P2 — Cao** | Hệ thống dừng hoàn toàn | Kill switch activated, tất cả workers error | Xử lý trong 30 phút |
| **P3 — Trung Bình** | Partial outage | 1 symbol lỗi, dashboard down | Xử lý trong 2 giờ |
| **P4 — Thấp** | Cảnh báo hiệu suất | Win rate thấp, model cũ | Xử lý ngày hôm sau |

### P1 — Quy Trình Xử Lý Khẩn Cấp

```
Phát hiện → Dừng giao dịch ngay → Đánh giá tổn thất → Bảo toàn vốn → Phân tích
```

**Bước 1 (< 1 phút):**
```bash
# Dừng tất cả Python ngay lập tức
taskkill /f /im python.exe
```

**Bước 2 (< 2 phút):**
- Vào MT5 → kiểm tra tất cả lệnh đang mở
- Đóng thủ công những lệnh nguy hiểm

**Bước 3 (< 5 phút):**
- Ghi lại: equity hiện tại, lệnh nào đang mở, log lúc xảy ra sự cố
- Screenshot dashboard và MT5

**Bước 4 (sau khi ổn định):**
- Phân tích log để tìm nguyên nhân
- Quyết định: sửa và restart, hay chờ phân tích kỹ hơn

---

### P2 — Kill Switch Activated

```
1. Đừng panic — đây là tính năng bảo vệ hoạt động đúng
2. Xem Dashboard → System tab → Kill reason
3. Xem log: Select-String signal_server.log -Pattern "Kill switch"
4. Xác nhận tất cả lệnh đã đóng trong MT5
5. Phân tích:
   - Nếu do news event 1 lần: reset sau khi thị trường bình thường
   - Nếu do performance: retrain trước khi reset
6. python main.py reset-kill → restart
```

---

### Quyết Định Reset vs Không Reset Kill Switch

| Tình huống | Quyết định | Lý do |
|---|---|---|
| Drawdown do Fed statement bất ngờ | Reset sau 2-4 giờ | Market freak event, model vẫn OK |
| Drawdown do chuỗi 10 lệnh thua liên tiếp | Retrain trước | Model có vấn đề |
| Drawdown do lot quá to (Kelly sai) | Điều chỉnh `KELLY_FRACTION` | Config issue |
| Drawdown không rõ nguyên nhân | Phân tích kỹ 24h | Không reset vội |

---

## Phụ Lục A: Health Check Commands

```bash
# Kiểm tra tất cả services đang chạy
tasklist | findstr python

# Kiểm tra port 5555
netstat -ano | findstr 5555

# Kiểm tra disk space
wmic logicaldisk get size,freespace,caption

# Kiểm tra RAM
wmic OS get TotalVisibleMemorySize,FreePhysicalMemory

# Xem live state ngay lập tức
type d:\xau_ats\logs\live_state.json

# Xem worker status
type d:\xau_ats\logs\worker_status.json
```

---

## Phụ Lục B: Windows Task Scheduler — Auto-Start

Để hệ thống tự khởi động sau reboot:

1. Mở Task Scheduler → Create Basic Task
2. Trigger: "When the computer starts" + delay 60s (để MT5 khởi động trước)
3. Action: Start a Program
   - Program: `d:\xau_ats\venv\Scripts\python.exe`
   - Arguments: `d:\xau_ats\main.py multi-live`
   - Start in: `d:\xau_ats`

**Lưu ý:** Bạn vẫn cần mở MT5 và gắn EA thủ công sau reboot — không có cách tự động hoàn toàn vì MT5 yêu cầu tương tác người dùng để đăng nhập.

---

## Phụ Lục C: Liên Hệ & Tài Nguyên

| Tài nguyên | Mô tả |
|---|---|
| `docs/technical_guide.md` | Tài liệu kỹ thuật chi tiết |
| `docs/user_guide.md` | Hướng dẫn sử dụng cơ bản |
| `tests/test_ats.py` | 38 test cases — chạy trước mỗi deployment |
| `config.py` | Tất cả tham số có chú thích |
| MT5 Help → MQL5 Reference | Tài liệu MQL5 chính thức |
