# Tài Liệu Kỹ Thuật: Hệ Thống Giao Dịch Tự Động XAU/USD (ATS)

> **Dành cho ai?** Tài liệu này được viết để người không có chuyên môn sâu về tài chính định lượng hay học máy có thể hiểu được hệ thống hoạt động như thế nào, tại sao chúng tôi chọn từng công nghệ, và những ưu/nhược điểm của chúng.

---

## Mục Lục

1. [Tổng Quan Hệ Thống](#1-tổng-quan-hệ-thống)
2. [Khung Thời Gian Và Chu Kỳ Giao Dịch](#2-khung-thời-gian-và-chu-kỳ-giao-dịch)
3. [Pipeline Dữ Liệu](#3-pipeline-dữ-liệu)
4. [Kỹ Thuật Đặc Trưng (Feature Engineering)](#4-kỹ-thuật-đặc-trưng)
5. [Mô Hình AI](#5-mô-hình-ai)
6. [Quản Lý Rủi Ro](#6-quản-lý-rủi-ro)
7. [Cơ Sở Hạ Tầng & Kết Nối MT5](#7-cơ-sở-hạ-tầng--kết-nối-mt5)
8. [Kiểm Thử Và Xác Nhận (Backtesting)](#8-kiểm-thử-và-xác-nhận)
9. [Giám Sát Thời Gian Thực](#9-giám-sát-thời-gian-thực)
10. [Hạn Chế Và Rủi Ro](#10-hạn-chế-và-rủi-ro)
11. [Hệ Thống Còn Thiếu Gì Để Thành Quant Trading Thực Thụ?](#11-hệ-thống-còn-thiếu-gì-để-thành-quant-trading-thực-thụ)

---

## 1. Tổng Quan Hệ Thống

### Hệ Thống Làm Gì?

ATS (Automated Trading System) là một hệ thống giao dịch tự động cho cặp XAU/USD (vàng so với đô la Mỹ). Nó quan sát dữ liệu giá theo thời gian thực từ MetaTrader 5, dùng trí tuệ nhân tạo để quyết định mua/bán/giữ, và gửi lệnh giao dịch trở lại MT5.

### Luồng Hoạt Động Tổng Quát

```
MT5 Terminal (dữ liệu giá)
        │
        ▼
Python: LiveTickStream (đọc tick từng giây)
        │
        ▼
Feature Engineering (tính 24 đặc trưng từ giá)
        │
        ▼
PPO Agent (AI quyết định: giữ / mua / bán / đóng)
        │
        ▼
Risk Manager (Kelly sizing, kill switch)
        │
        ▼
SignalServer → ZeroMQ → MT5 EA → Lệnh giao dịch
        │
        ▼
LiveStateWriter → live_state.json
        │
   ┌────┴────┐
   ▼         ▼
Streamlit  Telegram Bot
Dashboard  (cảnh báo)
```

### Kiến Trúc Đa Symbol

Hệ thống hỗ trợ nhiều cặp tiền tệ cùng lúc (XAUUSD, EURUSD, BTCUSD, ...). Mỗi cặp chạy trong một luồng (thread) riêng biệt, được quản lý bởi `multi_runner.py`. Khi bạn mở một chart mới trong MT5 và gắn EA `ATS_Panel`, hệ thống Python tự động phát hiện và khởi động một worker cho symbol đó.

---

## 2. Khung Thời Gian Và Chu Kỳ Giao Dịch

### 2.1 Hai Tầng Thời Gian

Hệ thống hoạt động trên **hai tầng thời gian** hoàn toàn khác nhau — đây là điểm dễ gây nhầm lẫn nhất cho người mới tìm hiểu:

| Tầng | Mục đích | Đơn vị | Tốc độ |
|---|---|---|---|
| **M1 bar (training)** | Huấn luyện AI, tính feature | 1 phút/bar | Offline, batch |
| **Tick (live)** | Ra quyết định thực, thực thi lệnh | ~0.5–2 giây/tick | Real-time |

### 2.2 Tầng 1: M1 Bars — Nền Tảng Huấn Luyện

**Dữ liệu training:** 50.000 bar M1 ≈ 35 ngày giao dịch liên tục (vàng giao dịch 24/5).

```
50.000 bars × 1 phút = 50.000 phút ÷ 1.440 phút/ngày ≈ 35 ngày
```

**Tại sao dùng M1 thay vì M5 hay H1?**
- M1 cho nhiều mẫu (sample) nhất để agent học từ → 50.000 tình huống vs 10.000 với M5
- Thời gian giữ lệnh tối đa là **60 bar M1 = 60 phút** → intraday, không qua đêm
- Granularity đủ để nắm bắt các move ngắn (scalp/intraday swing)

**Tại sao không dùng tick cho training?**
- Tick data quá nhiều noise, không ổn định (broker khác nhau có tick rate khác nhau)
- 50.000 tick chỉ ≈ 14 giờ — không đủ sample đa dạng cho AI học
- Feature engineering phức tạp hơn đáng kể, overfitting cao hơn

### 2.3 Tầng 2: Tick — Thực Thi Live

Khi hệ thống chạy live, `LiveTickStream` nhận từng **tick** từ MT5:

```
MT5 Tick (bid/ask mới) → Python nhận → Cập nhật deque(200) → AI predict → Tín hiệu
```

**Tần suất tick thực tế:**
- XAU/USD giờ London/NY: ~2-5 tick/giây
- XAU/USD giờ Asia ít thanh khoản: ~0.2-1 tick/giây
- Trung bình: ~1-2 tick/giây → mỗi quyết định cách nhau ~0.5-2 giây

**Feature recompute tối ưu:** Để tránh tính toán quá nhiều, feature chỉ được tính lại **mỗi 3 tick** (`FEAT_INTERVAL=3`). Feature của vàng không thay đổi đáng kể chỉ trong 1 tick (< 1 giây), nên việc này an toàn và giảm CPU load 3×.

### 2.4 Vòng Đời Một Lệnh Điển Hình

```
T=0:00  → AI nhận feature vector, predict action=1 (Long)
T=0:01  → Signal: {side=1, price=2005.50, sl=1998.00, tp=2018.50, lot=0.1}
T=0:01  → ZMQ publish → MT5 EA nhận → OrderSend() → Broker confirm
T=0:02  → live_state.json cập nhật: position=1, entry=2005.50
T=0:xx  → Mỗi tick: trailing stop cập nhật
T=45:00 → Giá chạm TP 2018.50 → MT5 tự động đóng lệnh
         HOẶC
T=60:00 → Hết max hold = 60 bar → AI gửi signal close (action=3)
```

### 2.5 Session Giao Dịch

| Session | Giờ GMT | Đặc điểm | Chiến lược phù hợp |
|---|---|---|---|
| Asia | 0h–8h | Thanh khoản thấp, range nhỏ | Mean-reversion |
| London | 8h–13h | Volatility cao, breakout | Trend-following |
| New York | 13h–21h | Volume lớn nhất/ngày | Cả hai |
| Overlap NY+London | 13h–16h | Tốt nhất cho XAU/USD | Đặc biệt chú ý |
| > 21h GMT | Stop no-new-trades | — | Không mở lệnh mới |
| 22h GMT | Kill switch | Đóng tất cả | — |

**Time encoding** trong feature (sin/cos của giờ GMT) giúp AI học được pattern theo session, dù không hardcode bất kỳ giờ cụ thể nào.

---

## 3. Pipeline Dữ Liệu

### 3.1 Nguồn Dữ Liệu: MetaTrader 5

MT5 là nền tảng giao dịch phổ biến cung cấp:
- **OHLC bars** (Open/High/Low/Close): dữ liệu nến giá theo khung thời gian (M1, M5, H1...)
- **Tick data**: từng thay đổi giá nhỏ nhất (bid/ask)

**Tại sao dùng MT5?**
- Miễn phí, có API Python chính thức (`MetaTrader5` package)
- Cho phép back-fill lịch sử 10+ năm
- Kết nối trực tiếp với broker thật sự

### 3.2 Cache Dữ Liệu (Parquet)

Sau khi tải dữ liệu từ MT5, hệ thống lưu vào file `.parquet` (định dạng cột, nén tốt). Lần sau sẽ đọc từ cache thay vì tải lại từ MT5, tiết kiệm thời gian đáng kể.

**Parquet vs CSV:**
| Tiêu chí | Parquet | CSV |
|---|---|---|
| Kích thước file | ~5× nhỏ hơn | Lớn |
| Tốc độ đọc | 10-50× nhanh hơn | Chậm |
| Hỗ trợ kiểu dữ liệu | Đầy đủ (datetime, float64) | Phải chuyển đổi |

### 3.3 Dữ Liệu Tổng Hợp (Synthetic Data)

Khi không có MT5 hoặc khi muốn train nhanh, hệ thống tạo dữ liệu giả theo mô hình **Geometric Brownian Motion + Ornstein-Uhlenbeck**:

```
Giá_{t+1} = Giá_t + θ·(μ - Giá_t)·dt + σ·Giá_t·dW
```

Trong đó:
- `θ` = tốc độ hồi quy về trung bình (mean-reversion speed)
- `μ` = giá trung bình dài hạn (long-run mean) = 2000 USD
- `σ` = biến động (volatility) = 15%/năm
- `dW` = nhiễu Gaussian (Brownian noise)

**Mục đích:** Huấn luyện agent ban đầu khi chưa có đủ dữ liệu thật. Dữ liệu tổng hợp có đặc tính thống kê tương tự XAU/USD nên agent học được các nguyên tắc cơ bản.

**Hạn chế:** Dữ liệu tổng hợp không có các sự kiện đột biến (news, fed meeting), không có microstructure thật (spread biến động), nên hiệu suất thực tế sẽ khác với backtest.

### 3.4 LiveTickStream

Trong chế độ live trading, `LiveTickStream` liên tục đọc tick mới nhất từ MT5 và duy trì một "cửa sổ trượt" (sliding window) gồm 200 tick gần nhất bằng `deque` (hàng đợi hai đầu):

```python
deque(maxlen=200)  # tự động xóa tick cũ khi đầy
```

**Tại sao dùng deque thay vì list?**
- `list.append() + list[-200:]` → O(N): phải sao chép toàn bộ danh sách mỗi lần
- `deque(maxlen=N).append()` → O(1): chỉ thêm 1 phần tử, tự động xóa phần tử cũ

---

## 4. Kỹ Thuật Đặc Trưng

**Feature engineering** là quá trình tính toán các con số có ý nghĩa từ dữ liệu giá thô để đưa vào mô hình AI. Giá thô (open/high/low/close) không đủ thông tin — AI cần các chỉ số đã được tính toán như "giá hiện tại cao hơn trung bình bao nhiêu?" hay "biến động hiện tại so bình thường thế nào?".

Tất cả 24 đặc trưng đều được tính theo dạng **rolling** (cửa sổ trượt), đảm bảo tại thời điểm t chỉ dùng dữ liệu ≤ t, không có **look-ahead bias** (gian lận tương lai).

### 4.1 OU Z-Score (Đặc Trưng #0)

**Khái niệm:** Quá trình Ornstein-Uhlenbeck (OU) mô tả một giá trị "dao động quanh trung bình" — giống như một lò xo: càng kéo xa trung bình, lực kéo về càng mạnh.

**Giải thích trực quan:**
- Nếu vàng thường giao dịch quanh 2000 USD, và hiện tại đang ở 2050 USD → z-score cao (+2) → có thể sẽ giảm
- Nếu vàng đang ở 1950 USD → z-score thấp (-2) → có thể sẽ tăng

**Công thức:**
```
z_score = (giá_hiện_tại - trung_bình_50_bar) / độ_lệch_chuẩn_50_bar
```

**Tại sao dùng OU/z-score?**
- XAU/USD có tính chất hồi quy trung bình (mean-reverting) trong ngắn hạn
- Z-score chuẩn hóa giá theo biến động, giúp AI so sánh được qua các thời kỳ khác nhau

### 4.2 OU Parameters MLE (Đặc Trưng #19-21)

Ngoài z-score đơn giản, hệ thống còn ước lượng tham số OU qua **Maximum Likelihood Estimation (MLE)** dùng hồi quy OLS (Ordinary Least Squares):

**Mô hình:** `ΔX = a + b·X_{t-1} + ε`

Từ đó suy ra:
- `θ` (theta) = tốc độ hồi quy = `-b/dt` → đặc trưng #19
- `μ` (mu) = trung bình dài hạn = `a/(θ·dt)` → dùng tính `ou_mu_dev` (#20)
- `σ` (sigma) = biến động OU
- **Half-life** = `ln(2)/θ` = thời gian để độ lệch giảm còn một nửa → đặc trưng #21

**Ví dụ:** Nếu half-life = 30 phút, nghĩa là nếu vàng lệch khỏi trung bình, sau 30 phút nó thường đã đi về được một nửa khoảng cách.

**Tối ưu hóa quan trọng:** Phiên bản cũ dùng vòng lặp Python + `numpy.linalg.lstsq` (O(N²) — mỗi cửa sổ tính riêng). Phiên bản mới dùng **rolling OLS vectorized** hoàn toàn (O(N)), nhanh hơn **1500 lần**:
- 50.000 bar cũ: ~50 giây
- 50.000 bar mới: ~0.033 giây

### 4.3 ATR — Average True Range (Đặc Trưng #1)

**Khái niệm:** ATR đo lường **biến động** của thị trường — giá dao động bao rộng trong một khoảng thời gian nhất định.

**True Range** của một nến = max của:
1. High - Low (biên độ trong nến)
2. |High - Close_trước| (gap lên)
3. |Low - Close_trước| (gap xuống)

**ATR** = trung bình True Range của 14 nến gần nhất.

**Dùng để làm gì?**
- Đặt Stop-Loss: `SL = Giá_vào ± 1.5 × ATR`
- Đặt Take-Profit: `TP = Giá_vào ± 3.0 × ATR` (tỷ lệ R:R = 2:1)
- Tính lot size (Kelly)
- Phát hiện thị trường biến động cao/thấp

**Tại sao dùng ATR thay vì % cố định?**
Khi thị trường bình thường, SL 10 pips là hợp lý. Khi có tin tức và thị trường biến động mạnh hơn 3× bình thường, SL 10 pips sẽ bị quét liên tục. ATR tự động điều chỉnh theo điều kiện thị trường thực tế.

### 4.4 VWAP — Volume Weighted Average Price (Đặc Trưng #2)

**Khái niệm:** VWAP là giá trung bình có tính đến khối lượng giao dịch — được reset mỗi ngày giao dịch.

```
VWAP = Σ(Giá × Khối_lượng) / Σ(Khối_lượng)
```

**Ý nghĩa:** VWAP thường được dùng như ranh giới phân tách xu hướng trong ngày:
- Giá > VWAP → áp lực mua, xu hướng tăng trong ngày
- Giá < VWAP → áp lực bán, xu hướng giảm trong ngày

**VWAP Deviation** (đặc trưng thực dùng):
```
vwap_dev = (Giá_đóng - VWAP) / ATR
```
Chuẩn hóa bằng ATR để AI so sánh được qua các điều kiện biến động khác nhau.

### 4.5 LOB Imbalance Proxy (Đặc Trưng #3)

**LOB** = Limit Order Book (sổ lệnh). **Imbalance** = mất cân bằng giữa lệnh mua và bán.

Vì dữ liệu LOB đầy đủ rất khó lấy từ MT5, hệ thống dùng **proxy** (xấp xỉ):
```
lob_imb = sign(Δgiá) × khối_lượng / (trung_bình_khối_lượng_50_bar)
```

**Ý nghĩa:** Nếu giá tăng với khối lượng lớn → tín hiệu mua mạnh. Nếu giá tăng với khối lượng nhỏ → tín hiệu yếu, có thể đảo chiều.

**Hạn chế:** Đây chỉ là xấp xỉ thô. LOB imbalance thực sự cần dữ liệu Level 2 (depth of market) mà phần lớn broker không cung cấp miễn phí.

### 4.6 Mã Hóa Thời Gian (Time Encoding, Đặc Trưng #8-9)

**Vấn đề:** AI cần "biết" thời gian trong ngày để hiểu pattern trading (London open 8h GMT, New York open 13h GMT, Asian close...). Nhưng số 14 (giờ) không nói lên rằng 23h và 1h gần nhau.

**Giải pháp:** Dùng sine/cosine để mã hóa circular (vòng tròn):
```
time_sin = sin(2π × giờ / 24)
time_cos = cos(2π × giờ / 24)
```

Với cách này:
- 23h và 1h gần nhau trong không gian (sin/cos)
- 0h và 12h xa nhau nhất
- Hai giá trị (sin, cos) đủ để tái dựng giờ ban đầu

### 4.7 Momentum (Đặc Trưng #5-7, 16-18)

**Log returns** qua nhiều khung thời gian:
```
mom_5  = ln(giá_hiện_tại / giá_5_bar_trước)
mom_15 = ln(giá_hiện_tại / giá_15_bar_trước)
mom_60 = ln(giá_hiện_tại / giá_60_bar_trước)
```

**Tại sao log return thay vì % return thông thường?**
- Log return có tính chất cộng theo thời gian: `r_total = r_1 + r_2 + ...`
- Phân phối gần chuẩn hơn (dễ xử lý hơn cho model)
- Đối xứng: tăng 10% rồi giảm 10% ≈ 0 theo log

### 4.8 Realized Volatility (Đặc Trưng #4)

```
rvol = std(log_returns_50_bar) × √(252 × 1440)
```

Annualized (quy về năm) để dễ so sánh qua các thời kỳ. Nhân `√(252 × 1440)` vì có 252 ngày giao dịch/năm, mỗi ngày 1440 phút.

**Ý nghĩa:** Nếu rvol cao → thị trường đang biến động mạnh → tín hiệu ít tin cậy hơn. Đặc trưng `vol_ratio` (#15) = rvol / trung_bình_100_bar cho biết biến động hiện tại cao/thấp hơn bình thường bao nhiêu lần.

---

## 5. Mô Hình AI

### 5.1 Tổng Quan: Học Tăng Cường (Reinforcement Learning)

Thay vì dạy AI theo dạng "if giá tăng thì mua", hệ thống dùng **Reinforcement Learning (RL)** — AI tự học thông qua thử nghiệm và phần thưởng:

```
Agent ──hành động──▶ Môi trường (thị trường)
  ▲                        │
  └────── phần thưởng ◀────┘
         (P&L, drawdown)
```

**Không gian hành động (Action Space):** 4 hành động rời rạc:
- `0` = Giữ nguyên (Hold)
- `1` = Mua (Long)
- `2` = Bán (Short)
- `3` = Đóng vị thế (Close)

**Phần thưởng (Reward):** Dựa trên P&L thực tế, trừ phạt cho drawdown và holding quá lâu (tối đa 60 bar).

**Observation:** Vector 24 đặc trưng đã tính ở trên.

### 5.2 PPO — Proximal Policy Optimization

**PPO** là thuật toán RL on-policy (học trực tiếp từ kinh nghiệm mới nhất), được OpenAI phát triển năm 2017. Đây là thuật toán **mặc định** của hệ thống.

**Ý tưởng cốt lõi:** Cập nhật policy (chiến lược) không quá mạnh trong một bước, tránh "quên" kiến thức cũ:
```
Mục tiêu = E[min(r_t × Â_t, clip(r_t, 1-ε, 1+ε) × Â_t)]
```
Trong đó:
- `r_t` = xác suất hành động mới / xác suất hành động cũ
- `Â_t` = Advantage (hành động này tốt hơn trung bình bao nhiêu)
- `ε` = 0.2 (clip range, mặc định)

**Ưu điểm PPO:**
- ✅ Ổn định, ít hyperparameter nhạy cảm
- ✅ Hoạt động tốt với action space rời rạc
- ✅ Dễ debug, training không bị phân kỳ
- ✅ Được dùng rộng rãi (benchmark tốt)

**Nhược điểm PPO:**
- ❌ On-policy: cần nhiều dữ liệu mới để học (sample-inefficient)
- ❌ Khó học chiến lược dài hạn (horizon dài)
- ❌ Kết quả có thể thay đổi giữa các lần train (stochastic)

**Cấu hình training:**
- 200.000 timesteps (~3 phút trên CPU)
- 50.000 bars dữ liệu
- Learning rate: 3×10⁻⁴
- Batch size: 64 episodes

### 5.3 SAC — Soft Actor-Critic

**SAC** là thuật toán RL off-policy (có thể học lại từ kinh nghiệm cũ), với thêm entropy regularization.

**Khác biệt so với PPO:**
- SAC dùng action space **liên tục** (Box[-1, 1]), rồi mapping sang 4 hành động rời rạc:
  - action < -0.5 → Short
  - -0.5 ≤ action < 0 → Close
  - 0 ≤ action < 0.5 → Hold
  - action ≥ 0.5 → Long
- Dùng **replay buffer** — lưu kinh nghiệm cũ và học lại → sample-efficient hơn

**Ưu điểm SAC:**
- ✅ Sample-efficient: cần ít dữ liệu hơn để đạt cùng hiệu suất
- ✅ Entropy bonus khuyến khích exploration → ít bị stuck ở local optimum
- ✅ Off-policy: có thể dùng lại dữ liệu cũ

**Nhược điểm SAC:**
- ❌ Phức tạp hơn (3 mạng: actor, critic, value)
- ❌ Nhạy cảm với hyperparameter hơn PPO
- ❌ Continuous action → discrete mapping có thể mất thông tin

**Khi nào dùng SAC thay PPO?**
Khi bạn có ít dữ liệu training hoặc muốn fine-tune model đã có.

### 5.4 T-KAN — Temporal Kolmogorov-Arnold Network

**T-KAN** là mô hình phân loại **regime thị trường**: xác định thị trường đang ở trạng thái gì.

**Hai regime:**
- `0` = RANGE (thị trường đi ngang, mean-reverting) → chiến lược phù hợp: bán khi quá cao, mua khi quá thấp
- `1` = TREND (thị trường có xu hướng rõ) → chiến lược phù hợp: theo trend, đừng fade

**Kiến trúc T-KAN:**
```
Input (50 bar × 6 features)
    │
ChebyshevBasis (polynomial expansion, bậc 4)
    │
GRU (Gated Recurrent Unit) — xử lý chuỗi thời gian
    │
Linear Classifier → Softmax
    │
Output: [P(range), P(trend)]
```

**KAN vs MLP (Mạng Nơ-ron Thông Thường):**
- MLP: `y = σ(W·x + b)` — học trọng số tuyến tính, activation cố định
- KAN: `y = Σ φ_ij(x_i)` — học hàm phi tuyến trên từng kết nối

**Tại sao dùng Chebyshev Basis?**
Chebyshev polynomials có tính chất tối ưu trong việc xấp xỉ hàm số trên đoạn [-1, 1] (giảm thiểu Runge's phenomenon). Bậc 4 đủ để nắm bắt phi tuyến mà không overfit.

**Ưu điểm T-KAN:**
- ✅ Phù hợp cho time series với cấu trúc phi tuyến
- ✅ GRU giữ memory ngắn hạn (không bị vanishing gradient như LSTM "đầy đủ")
- ✅ Nhẹ, training nhanh (vài giây)

**Nhược điểm T-KAN:**
- ❌ Regime classification là bài toán khó, accuracy ~60-65% trên thực tế
- ❌ Nhãn regime được tạo tự động (label_regimes) — không hoàn hảo
- ❌ KAN còn mới (2024), ít tài liệu và benchmark

**Hiện tại:** T-KAN được tích hợp nhưng **không bắt buộc** (`regime_model=None` → mặc định regime=0). Phần thưởng agent đã mã hóa regime gián tiếp qua z-score và momentum.

### 5.5 Lý Do Chọn Kiến Trúc Này

| Lựa chọn thay thế | Lý do không chọn |
|---|---|
| LSTM thuần túy | Khó train, cần nhiều dữ liệu hơn, không tốt hơn RL trên action selection |
| Transformer | Quá nặng cho live inference (độ trễ ~100ms không chấp nhận được) |
| Rule-based (MA crossover) | Không thích nghi được với regime changes |
| XGBoost/Random Forest | Không học được sequential decision making |
| DQN | Kém ổn định hơn PPO, không hội tụ tốt trên time series tài chính |

---

## 6. Quản Lý Rủi Ro

### 6.1 Fractional Kelly Criterion

**Vấn đề cơ bản:** Đặt cược bao nhiêu? Đặt quá ít → bỏ lỡ cơ hội. Đặt quá nhiều → phá sản.

**Kelly Criterion** (John Kelly, 1956) cho bạn tỷ lệ vốn tối ưu để đặt cược:
```
f* = (p × b - q) / b
```
Trong đó:
- `p` = xác suất thắng
- `q` = 1 - p = xác suất thua
- `b` = tỷ lệ thắng/thua (Risk-Reward ratio)

**Ví dụ:** p=0.55, RR=2 → `f* = (0.55×2 - 0.45)/2 = 0.325` = 32.5% vốn

**Vấn đề với Kelly đầy đủ:** 32.5% là quá rủi ro! Kelly đầy đủ tối ưu về tăng trưởng dài hạn nhưng có variance rất cao — drawdown 50-70% là bình thường.

**Fractional Kelly:** Hệ thống dùng 1/10 Kelly:
```python
fraction = 1/10  # chỉ dùng 10% Kelly
f_actual = fraction × f*
lot_size = (account_equity × f_actual × MAX_RISK_PER_TRADE) / sl_distance
```

**Giới hạn thêm:**
- Tối đa 2% vốn/lệnh (`MAX_RISK_PER_TRADE = 0.02`)
- Kelly được cập nhật liên tục từ win/loss gần đây (rolling estimate)

**Ưu điểm:** Tự động giảm size khi thị trường bất lợi (win rate giảm → Kelly giảm → lot nhỏ lại)

### 6.2 Kill Switch (Ngắt Khẩn Cấp)

Hệ thống có **tự động ngắt** khi tổn thất vượt ngưỡng:

```python
MAX_DRAWDOWN_PCT = 15.0  # ngắt khi drawdown > 15%
NO_NEW_TRADES_HOUR = 21  # không mở lệnh mới sau 21h GMT
EOD_HOUR_GMT = 22        # đóng tất cả lệnh lúc 22h GMT
```

**Các trigger kill switch:**
1. **Drawdown > 15%:** Đóng tất cả vị thế, dừng giao dịch mới
2. **End-of-day (22h GMT):** Đóng hết trước khi thị trường ít thanh khoản
3. **No-new-trades (21h GMT):** Có thể giữ lệnh đã mở nhưng không mở lệnh mới

**Tại sao 22h GMT?** Vàng giao dịch 24/5. Sau 22h GMT, thanh khoản giảm mạnh (thị trường Mỹ đóng), spread rộng, tín hiệu kém tin cậy.

### 6.3 ATR Trailing Stop

Sau khi vào lệnh, stop-loss **tự động di chuyển** theo chiều có lợi (chỉ đi một chiều, không lui):

```python
# Long position:
trail_stop = max(trail_stop, mid_price - mult × ATR)

# Short position:
trail_stop = min(trail_stop, mid_price + mult × ATR)
```

**Ví dụ thực tế:**
- Mua vàng ở 2000, ATR=5, mult=2 → SL ban đầu = 2000 - 10 = 1990
- Vàng lên 2015 → SL di chuyển lên 2015 - 10 = 2005 (bảo vệ lợi nhuận)
- Vàng lên 2030 → SL di chuyển lên 2030 - 10 = 2020
- Vàng giảm về 2021 → SL kích hoạt tại 2020, chốt lãi 20 điểm

**Lợi ích:** Cho phía lệnh chạy (let profits run) trong khi bảo vệ lợi nhuận đã có.

### 6.4 VWAP Slicing

Với lệnh lớn (lot > 0.5), thay vì đặt một lệnh duy nhất (có thể ảnh hưởng giá), hệ thống chia nhỏ ra nhiều lệnh xung quanh VWAP:

```python
if lot > VWAP_SLICE_THRESHOLD:
    slices = vwap_slice_orders(lot, vwap_price, atr)
    # Chia thành ~3 lệnh nhỏ hơn
```

**Tại sao quan trọng?** Market impact — khi đặt lệnh lớn, bạn "đẩy" giá đi không tốt cho mình. Chia nhỏ giúp thực thi (execution) tốt hơn, đặc biệt với vàng vào giờ thanh khoản thấp.

---

## 7. Cơ Sở Hạ Tầng & Kết Nối MT5

### 7.1 ZeroMQ — Kênh Truyền Tín Hiệu

**ZeroMQ** (ZMQ) là thư viện messaging hiệu năng cao. Hệ thống dùng pattern **PUB/SUB**:

```
Python SignalServer (PUBLISHER)
    └── bind tcp://127.0.0.1:5555
         │
         ├── topic=XAUUSD → {side=1, lot=0.1, sl=1990, tp=2020}
         ├── topic=EURUSD → {side=-1, lot=0.05, ...}
         └── topic=HEARTBEAT → {timestamp=...}
                 │
MT5 EA (SUBSCRIBER)
    └── connect tcp://127.0.0.1:5555
         └── subscribe topic=XAUUSD (chỉ nhận tín hiệu của mình)
```

**Topic = Symbol:** Mỗi EA đăng ký đúng symbol của mình → EA XAUUSD không nhận tín hiệu EURUSD và ngược lại. Điều này cho phép nhiều EA trên nhiều symbol dùng chung **một cổng**.

**Tại sao ZMQ thay vì HTTP/REST?**
| | ZMQ PUB/SUB | HTTP REST |
|---|---|---|
| Độ trễ | < 1ms | 5-20ms |
| Throughput | Hàng triệu/giây | Hàng nghìn/giây |
| Độ phức tạp | Thấp | Thấp |
| Hướng kết nối | Publisher → nhiều Subscriber | Client → Server |

Trong giao dịch, độ trễ tín hiệu quan trọng — 10ms chậm có thể là 5-10 pips thua thiệt khi market moving fast.

**Heartbeat:** Server gửi `{heartbeat: True}` mỗi 5 giây. EA theo dõi — nếu không nhận heartbeat > 30 giây → báo lỗi, không giao dịch.

**File Fallback:** Nếu ZMQ không hoạt động (lỗi kết nối), tín hiệu được ghi ra file JSON `{symbol}_signal.json` trong MT5 Common Files, EA đọc file này thay thế.

### 7.2 ATS_Panel.mq5 — Expert Advisor MT5

EA (Expert Advisor) là đoạn code MQL5 chạy trong MT5, nhận tín hiệu và đặt lệnh thật sự.

**Chức năng chính:**
1. **Đăng ký chart:** Ghi `ats_chart_{SYMBOL}.txt = "1"` khi OnInit, ghi "0" khi OnDeinit
2. **Đọc tín hiệu:** Subscribe ZMQ hoặc đọc file JSON
3. **Đặt lệnh:** `OrderSend()` với tất cả tham số (lot, SL, TP, magic number)
4. **Panel UI:** Hiển thị trạng thái tín hiệu gần nhất trên chart

**Auto-registration mechanism:** Khi bạn kéo EA vào chart EURUSD, EA ghi file `ats_chart_EURUSD.txt = "1"`. Python `multi_runner.py` quét thư mục mỗi 5 giây, phát hiện file mới → tự động khởi động worker cho EURUSD.

### 7.3 LiveStateWriter — Chia Sẻ Trạng Thái

`LiveStateWriter` ghi file `live_state.json` để Streamlit và Telegram bot đọc:

```json
{
  "_account": {"equity": 10000.0, "balance": 9800.0, "drawdown_pct": 2.0},
  "_system": {"alive": true, "killed": false, "signal_count": 47, ...},
  "XAUUSD": {"position": 1, "entry_price": 2005.5, "unrealized_pnl": 15.2, ...},
  "EURUSD": {"position": 0, ...}
}
```

**Atomic write (ghi nguyên tử):** Thay vì ghi thẳng vào file (reader có thể đọc file đang ghi dở), hệ thống ghi vào file `.tmp` rồi `rename`:
```python
tmp.write_text(payload)
tmp.replace(target)  # atomic rename — reader luôn thấy file hoàn chỉnh
```

**_dirty flag:** Để tránh tốn I/O không cần thiết, `flush()` chỉ ghi khi có `update_*()` được gọi từ lần ghi cuối.

### 7.4 Multi-Runner — Quản Lý Đa Symbol

`multi_runner.py` là orchestrator quan trọng:

```
Mỗi 5 giây:
  scan ats_chart_*.txt → danh sách symbol đang active
  
  Với mỗi symbol mới:
    Spawn SymbolWorker thread:
      1. load_ppo(symbol)  ← cố gắng load model đã train
      2. Nếu không có → _auto_train()
         a. Tải 50.000 bar từ MT5 (hoặc synthetic)
         b. Train PPO 200.000 timesteps (~3-5 phút)
         c. Lưu model
      3. Khởi động run_live_loop()
  
  Với symbol đã đóng chart:
    stop_event.set() → worker thread tự dừng
```

**WorkerStatus:** Mỗi worker báo trạng thái: `waiting | training | live | error`. Streamlit dashboard hiển thị badge tương ứng.

---

## 8. Kiểm Thử Và Xác Nhận

### 8.1 Walk-Forward Backtest

**Backtest thông thường (in-sample):** Train và test trên cùng dữ liệu → overfitting, kết quả ảo.

**Walk-forward backtest:** Mô phỏng cách hệ thống thực sự hoạt động qua thời gian:

```
|──Train fold 1──|──Test 1──|
               |──Train fold 2──|──Test 2──|
                              |──Train fold 3──|──Test 3──|
                                             ...
```

Mỗi fold:
1. Train agent trên N tháng dữ liệu
2. Test trên M tháng tiếp theo (30 ngày)
3. Tính TCA (Transaction Cost Analysis)
4. Chuyển cửa sổ tiến về phía trước

**Tại sao walk-forward quan trọng?**
Thị trường thay đổi theo thời gian (regime shifts). Model train năm 2020 có thể kém hiệu quả năm 2023. Walk-forward kiểm tra: liệu chiến lược có robust qua nhiều giai đoạn thị trường khác nhau?

### 8.2 TCA — Transaction Cost Analysis

Mỗi fold tính:
- Sharpe Ratio = `(return - risk_free) / std(returns)` (đo risk-adjusted return)
- Max Drawdown = tổn thất tối đa từ đỉnh (peak-to-trough)
- Win Rate = % lệnh có lời
- Profit Factor = tổng lãi / tổng lỗ
- Lookahead Bias Detection: kiểm tra các đặc trưng có dùng dữ liệu tương lai không

### 8.3 Test Suite Tự Động

`tests/test_ats.py` gồm 38 test cases:
- Feature engineering (OU MLE, ATR, VWAP, momentum...)
- RL environment (action space, reward function)
- Risk management (Kelly, kill switch)
- Data pipeline (load/fetch, cache)
- Signal server (publish, heartbeat)

**Tất cả 38 test pass** — đảm bảo các thay đổi code không phá vỡ chức năng cũ.

---

## 9. Giám Sát Thời Gian Thực

### 9.1 Streamlit Dashboard

Dashboard web tại `http://localhost:8501`:

**Sidebar:**
- Badge trạng thái: 🟢 LIVE / 🔴 KILLED / ⚪ OFFLINE
- Equity, Balance, Session P&L
- Gauge drawdown (xanh < 5%, cam 5-10%, đỏ > 10%)

**Tab mỗi symbol:**
- Trạng thái: ⏳ training / 🟢 live / ⚪ waiting / 🔴 error
- Regime, Position, Kelly f*, Drawdown
- Equity curve (session)
- Chi tiết vị thế đang mở
- Log tail liên quan symbol đó

**Auto-refresh:** Mỗi 5 giây tự reload.

### 9.2 Telegram Bot

Bot gửi cảnh báo tự động khi:
- Kill switch kích hoạt
- Lệnh mở/đóng
- Drawdown vượt 5%
- Heartbeat mất > 45 giây

**Commands:**
- `/status` — trạng thái hệ thống + equity
- `/positions` — vị thế đang mở
- `/stats` — thống kê tổng hợp

---

## 10. Hạn Chế Và Rủi Ro

### 10.1 Rủi Ro Mô Hình (Model Risk)

**Overfitting:** Model học thuộc dữ liệu lịch sử nhưng không tổng quát hóa được. Dấu hiệu: backtest tốt nhưng live trading kém.

**Giải pháp thực hiện:** Walk-forward validation, synthetic fallback training, FEAT_INTERVAL=3 (giảm overfit vào noise).

**Regime change:** Thị trường thay đổi đặc tính. Model train năm 2022 (period lãi suất tăng) có thể kém hiệu quả năm 2025.

**Giải pháp khuyến nghị:** Retrain định kỳ (weekly/monthly), monitor performance metrics liên tục.

### 10.2 Rủi Ro Kỹ Thuật

**Kết nối đứt:** MT5 disconnect, ZMQ timeout, Python crash.
- **Mitigation:** Heartbeat monitoring, file fallback, auto-reconnect trong LiveTickStream

**Slippage:** Giá lệnh mong muốn vs giá thực thi. Đặc biệt nguy hiểm khi lot lớn hoặc thanh khoản thấp.
- **Mitigation:** VWAP slicing, EOD close, không giao dịch sau 21h GMT

**Data quality:** Tick sai (outlier, gap), MT5 trả về `None`.
- **Mitigation:** `nan_to_num` trong feature pipeline, reconnect logic trong LiveTickStream

### 10.3 Hạn Chế Synthetic Data

Dữ liệu GBM+OU tổng hợp thiếu:
- **News events:** Non-farm payroll, Fed meetings → giá nhảy đột ngột
- **Spread biến động:** Trong thực tế spread có thể tăng 10× lúc news
- **Microstructure:** Order book dynamics, iceberg orders
- **Correlation với tài sản khác:** Vàng phụ thuộc USD index, yields

Model train chỉ trên synthetic data sẽ kém hiệu suất với các sự kiện đặc biệt này.

### 10.4 Về Kelly Criterion

Kelly criterion giả định:
- Biết được win rate thật sự (p)
- Biết được R:R thật sự (b)
- Các lệnh độc lập nhau

Trong thực tế, cả p và b đều là ước lượng có noise. Fractional Kelly (1/10) bù đắp cho sự không chắc chắn này.

### 10.5 Tóm Tắt Rủi Ro

| Rủi ro | Mức độ | Mitigation |
|---|---|---|
| Overfitting | Cao | Walk-forward, retrain định kỳ |
| Regime change | Cao | T-KAN regime detection, adaptive kelly |
| Kết nối MT5 | Thấp | Auto-reconnect, file fallback |
| Slippage cao | Trung bình | VWAP slicing, không trade giờ thấp thanh khoản |
| Kill switch không kích hoạt | Thấp | Test suite 38 cases |
| Dữ liệu synthetic kém | Trung bình | Ưu tiên train với dữ liệu MT5 thật |

---

## 11. Hệ Thống Còn Thiếu Gì Để Thành Quant Trading Thực Thụ?

> Đây là câu hỏi quan trọng nhất. Hệ thống ATS hiện tại là một **prototype có thể vận hành được**, nhưng còn khoảng cách đáng kể so với các hệ thống quant trading chuyên nghiệp (hedge fund, prop firm). Dưới đây là phân tích chi tiết theo từng lĩnh vực.

---

### 11.1 Tích Hợp Tin Tức & Rủi Ro Sự Kiện

**Vấn đề hiện tại:** Hệ thống hoàn toàn mù trước các sự kiện kinh tế.

Các sự kiện có thể gây vàng biến động 30-80 pips trong vài phút:
- **Fed FOMC** (8 lần/năm): Quyết định lãi suất, dot plot
- **Non-Farm Payrolls** (mỗi thứ 6 tuần 1 tháng): Con số việc làm Mỹ
- **CPI, PPI** (hàng tháng): Lạm phát Mỹ
- **Geopolitical events**: Xung đột, khủng hoảng tài chính
- **Dollar Index (DXY)**: Tương quan nghịch mạnh với vàng

**Tác hại thực tế:** Model đang ở long, Fed tăng lãi suất → vàng giảm ngay 50 pips → kill switch mới kích hoạt nhưng đã quá muộn. Slippage khi news có thể gấp 10-20× bình thường.

**Giải pháp cần thêm:**
```python
# Ý tưởng: News-aware position management
class EconomicCalendar:
    def get_upcoming_events(self, symbol, lookahead_hours=2) -> list[Event]:
        """Trả về các sự kiện high-impact trong 2h tới."""
        ...
    
    def should_flatten(self, symbol) -> bool:
        """Đóng tất cả vị thế nếu có high-impact event trong 30 phút."""
        ...
```

**Nguồn dữ liệu:** Forex Factory API, Bloomberg Economic Calendar, Investing.com economic calendar, hoặc paid providers (Refinitiv, Bloomberg).

**Ưu tiên:** **Rất cao** — đây là rủi ro lớn nhất của hệ thống hiện tại.

---

### 11.2 Mô Hình Chi Phí Giao Dịch Thực Tế (Realistic TCA)

**Vấn đề hiện tại:** Backtest và training dùng chi phí đơn giản (spread cố định).

**Chi phí thực tế gồm:**
| Chi phí | Mô tả | Ảnh hưởng điển hình (XAU/USD) |
|---|---|---|
| Bid-Ask Spread | Spread broker điển hình | ~3-5 pips = ~$0.03-0.05 |
| Slippage | Giá thực thi vs giá quoted | 1-50 pips tùy thanh khoản |
| Commission | Phí giao dịch broker | ~$3-7/lot |
| Overnight swap | Phí giữ lệnh qua đêm | Biến động, có thể âm |
| Market impact | Ảnh hưởng lệnh lớn lên giá | Đáng kể với lot > 1.0 |

**Spread biến thiên:**
- Giờ bình thường: 3-5 pips
- Giờ news: 20-100+ pips
- Gap qua đêm: 10-30 pips
- Weekend gap: Unpredictable

**Cần thêm:**
```python
class RealisticTCA:
    def apply_costs(self, price, side, lot, hour_utc) -> float:
        spread = self._dynamic_spread(hour_utc)  # tính spread theo giờ
        slippage = self._estimate_slippage(lot)   # market impact
        commission = lot * self.commission_per_lot
        return price + side * (spread/2 + slippage) + commission
```

**Ưu tiên:** **Cao** — không có realistic TCA, backtest luôn lạc quan giả.

---

### 11.3 Quản Lý Danh Mục & Tương Quan (Portfolio Risk)

**Vấn đề hiện tại:** Mỗi symbol được quản lý độc lập, không biết tương quan nhau.

**Tương quan quan trọng:**
- XAUUSD và DXY: tương quan **âm** mạnh (-0.7 đến -0.9)
- XAUUSD và BTCUSD: tương quan **dương** vừa (+0.3 đến +0.6) trong risk-off
- XAUUSD và US10Y Yield: tương quan **âm** (-0.5 đến -0.8)

**Tác hại thực tế:** Cùng lúc long XAU và long BTC (cả hai theo USD inverse) → tổng rủi ro gấp đôi, nhưng Kelly tính riêng từng symbol và nhân đôi lot size.

**Cần thêm:**
```python
class PortfolioRiskManager:
    def adjust_lots_for_correlation(
        self, symbol_lots: dict[str, float]
    ) -> dict[str, float]:
        """Giảm lot size tổng thể khi các symbol tương quan cao."""
        corr_matrix = self._rolling_correlation(symbols, window=200)
        effective_var = self._portfolio_variance(symbol_lots, corr_matrix)
        if effective_var > MAX_PORTFOLIO_VAR:
            scale = MAX_PORTFOLIO_VAR / effective_var
            return {s: lot * scale for s, lot in symbol_lots.items()}
        return symbol_lots
```

**Ưu tiên:** **Trung bình-Cao** — quan trọng khi chạy > 2 symbols cùng lúc.

---

### 11.4 Phát Hiện Model Drift & Tự Động Retrain

**Vấn đề hiện tại:** Model train một lần, chạy mãi. Không có cơ chế phát hiện khi model kém đi.

**Model drift** xảy ra khi:
- Thị trường thay đổi regime (ví dụ: từ low-vol 2021 sang high-vol 2022)
- Broker thay đổi spread, liquidity profile
- Macro regime shift (QE → QT, low rate → high rate)

**Dấu hiệu cần monitor:**
```python
class ModelDriftDetector:
    """Theo dõi performance decay theo thời gian."""
    
    def check_drift(self, recent_trades: list[Trade]) -> DriftReport:
        rolling_sharpe = self._rolling_sharpe(recent_trades, window=100)
        win_rate_decay = self._win_rate_trend(recent_trades)
        
        if rolling_sharpe < SHARPE_WARNING_THRESHOLD:
            return DriftReport(severity="WARNING", action="consider_retrain")
        if rolling_sharpe < SHARPE_CRITICAL_THRESHOLD:
            return DriftReport(severity="CRITICAL", action="pause_trading")
```

**Schedule retrain nên có:**
- **Weekly:** Retrain T-KAN regime classifier (đơn giản, nhanh)
- **Monthly:** Retrain PPO agent với dữ liệu mới nhất (thêm vào, không xóa cũ)
- **Triggered:** Khi win rate < 40% trong 50 lệnh liên tiếp → auto-retrain

**Ưu tiên:** **Cao** — đây là lý do phần lớn algo trader thất bại dài hạn.

---

### 11.5 Dữ Liệu Vi Cấu Trúc (Market Microstructure)

**Vấn đề hiện tại:** Chỉ dùng OHLC + tick volume. Thiếu thông tin về cấu trúc lệnh thực.

**Dữ liệu Level 2 (Order Book) — quan trọng cho vàng:**
- **Bid/Ask depth:** Có bao nhiêu lệnh ở mỗi mức giá
- **Order flow imbalance:** Số lệnh mua vs bán thực sự (không phải proxy)
- **Trade print:** Lệnh khớp thực tế (buyer-initiated vs seller-initiated)
- **Footprint chart:** Phân tích volume tại từng mức giá trong candlestick

**LOB features quan trọng:**
```python
# Dữ liệu Level 2 thực sự (cần broker cao cấp)
lob_bid_volume_5 = sum(bid_depth[:5])    # tổng volume 5 mức bid gần nhất
lob_ask_volume_5 = sum(ask_depth[:5])    # tổng volume 5 mức ask gần nhất
lob_imbalance = (lob_bid_volume_5 - lob_ask_volume_5) / (lob_bid_volume_5 + lob_ask_volume_5)
```

**Nguồn:** Cần broker cung cấp DOM (Depth of Market) qua API — không phải tất cả MT5 broker đều có.

**Ưu tiên:** **Trung bình** — cải thiện edge nhưng không critical.

---

### 11.6 Thuật Toán Thực Thi Lệnh (Execution Algorithms)

**Vấn đề hiện tại:** Đặt một lệnh market order duy nhất → giá thực thi kém khi lot lớn.

**Thuật toán execution chuyên nghiệp:**

| Thuật toán | Mô tả | Phù hợp khi |
|---|---|---|
| **TWAP** | Chia lệnh đều theo thời gian | Muốn phân phối đều execution |
| **VWAP** | Chia lệnh theo volume profile | Muốn execution gần VWAP ngày |
| **IS (Implementation Shortfall)** | Tối thiểu hóa deviation từ decision price | Khi urgency cao |
| **Iceberg** | Chỉ show một phần lệnh | Giấu size thật, tránh front-running |
| **POV (Percent of Volume)** | Tham gia X% volume thị trường | Khi không muốn move market |

Hệ thống hiện có VWAP slicing đơn giản (`vwap_slice_orders`) nhưng chưa có feedback loop (không biết execution quality).

**Cần thêm:**
```python
class ExecutionAnalytics:
    def measure_slippage(self, intended_price, executed_price, side) -> float:
        """Đo slippage thực tế mỗi lệnh để cải thiện execution."""
        return side * (executed_price - intended_price)
    
    def execution_shortfall(self, decision_price, executed_fills) -> float:
        """IS = Σ(fill_price - decision_price) × fill_size / total_size"""
```

**Ưu tiên:** **Trung bình** — quan trọng hơn khi lot size tăng lên.

---

### 11.7 Alternative Data

**Dữ liệu thay thế** là nguồn thông tin không phải giá/volume truyền thống, có thể cho edge.

**Phù hợp với vàng:**

| Nguồn | Thông tin | Độ trễ | Chi phí |
|---|---|---|---|
| COT Report | Vị thế long/short theo nhóm trader | 1 tuần | Miễn phí (CFTC) |
| ETF Flows | Vàng vào/ra ETF (GLD, IAU) | Ngày | Miễn phí |
| Gold lease rates | Chi phí mượn vàng vật chất | Ngày | Paid |
| Central bank flows | Ngân hàng trung ương mua/bán vàng | Quý | IMF IFS |
| Sentiment (NLP) | Phân tích tin tức, social media | Giây-Phút | Paid |
| Options IV | Implied volatility từ thị trường options | Real-time | Paid |

**Ví dụ tích hợp COT:**
```python
# COT = Commitments of Traders (CFTC, mỗi thứ 3 hàng tuần)
cot_feature = (managed_money_long - managed_money_short) / open_interest
# Khi managed money net long cực cao → thị trường "crowded" → reversal risk
```

**Ưu tiên:** **Thấp-Trung bình** — thêm edge nhỏ, nhưng phức tạp hóa đáng kể.

---

### 11.8 Kiểm Soát Rủi Ro Danh Mục Nâng Cao

**Vấn đề hiện tại:** Kill switch chỉ dựa trên drawdown % đơn giản.

**Quản lý rủi ro chuyên nghiệp cần:**

```
1. VaR (Value at Risk): Với confidence 95%, tôi có thể mất tối đa bao nhiêu
   trong ngày tiếp theo?
   
2. Expected Shortfall (CVaR): Trung bình tổn thất khi vượt VaR (đuôi phân phối)

3. Stress testing: Nếu xảy ra sự kiện như 2008, 2020, 2022... portfolio này
   sẽ chịu được không?

4. Regime-conditional limits: Giảm lot 50% khi regime=TREND và vol cao
   (vì RL agent train chủ yếu trên trung bình, không phải tail events)

5. Liquidity-adjusted position sizing: Không mở lệnh khi spread > 3×
   bình thường (giờ news)
```

**Ưu tiên:** **Cao** — cần thiết trước khi tăng capital đáng kể.

---

### 11.9 Audit Trail & Compliance

**Vấn đề hiện tại:** Log giao dịch cơ bản, không có audit trail đầy đủ.

**Hệ thống quant chuyên nghiệp cần:**

1. **Immutable trade log:** Mỗi lệnh được ghi với decision snapshot đầy đủ (feature vector tại thời điểm quyết định, model version, parameters)

2. **Model versioning:** Biết chính xác model nào đã ra quyết định nào (MLflow, DVC)

3. **P&L attribution:** Lợi nhuận đến từ đâu? Alpha từ model hay beta từ market direction?

4. **Replay capability:** Có thể tái hiện lại mọi quyết định với dữ liệu lúc đó

5. **Regulatory compliance:** Nếu quản lý tiền người khác → MiFID II, SEC reporting

**Ưu tiên:** **Trung bình** (cá nhân) → **Rất cao** (nếu quản lý vốn bên ngoài).

---

### 11.10 Tóm Tắt: Roadmap Để Trở Thành Quant Trading Thực Thụ

| Hạng mục thiếu | Mức độ cần thiết | Độ phức tạp | Ưu tiên |
|---|---|---|---|
| News/Event calendar | Bắt buộc | Trung bình | **#1** |
| Model drift detection & retrain | Bắt buộc | Trung bình | **#2** |
| Realistic TCA (dynamic spread, slippage) | Bắt buộc | Thấp | **#3** |
| Portfolio correlation risk | Quan trọng | Trung bình | **#4** |
| Advanced risk limits (VaR, stress test) | Quan trọng | Cao | **#5** |
| Market microstructure (Level 2) | Tốt cần có | Cao | **#6** |
| Execution algorithms | Tốt cần có | Cao | **#7** |
| Audit trail & model versioning | Quan trọng nếu scale | Thấp | **#8** |
| Alternative data | Tùy chọn | Rất cao | **#9** |

**Khoảng cách lớn nhất hiện tại:** Không có news awareness (#1) và không có model monitoring (#2). Đây là hai nguyên nhân hàng đầu khiến một hệ thống algo tốt về mặt kỹ thuật vẫn thất bại trong thực tế dài hạn.

---

## Appendix A: Các Tham Số Quan Trọng

| Tham số | Giá trị | Ý nghĩa |
|---|---|---|
| `FEATURE_DIM` | 24 | Số đặc trưng đầu vào cho AI |
| `ROLLING_WINDOW` | 200 | Cửa sổ OU MLE |
| `MAX_DRAWDOWN_PCT` | 15% | Ngưỡng kill switch |
| `KELLY_FRACTION` | 0.1 = 1/10 | Phân số Kelly |
| `MAX_RISK_PER_TRADE` | 2% | Rủi ro tối đa/lệnh |
| `AUTO_TRAIN_BARS` | 50.000 | Số bar cho auto-train |
| `AUTO_TRAIN_TIMESTEPS` | 200.000 | Số bước train PPO |
| `FEAT_INTERVAL` | 3 | Tính feature mỗi 3 tick |
| `SCAN_INTERVAL` | 5s | Tần suất quét chart mới |
| `HB_INTERVAL` | 5s | Tần suất heartbeat ZMQ |
| `HB_WARN_SEC` | 45s | Cảnh báo mất heartbeat |

## Appendix B: Cấu Trúc Thư Mục

```
d:/xau_ats/
├── ai_models/
│   ├── checkpoints/        ← Models đã train (*.zip)
│   ├── features.py         ← 24 đặc trưng
│   ├── rl_agent.py         ← PPO/SAC train & load
│   ├── regime_tkan.py      ← T-KAN classifier
│   └── trading_env.py      ← RL environment (gym)
├── backtest/
│   └── walkforward.py      ← Walk-forward engine
├── config.py               ← Tất cả tham số tập trung
├── dashboard/
│   ├── app.py              ← Streamlit UI
│   ├── state_reader.py     ← Parse live_state.json
│   └── telegram_bot.py     ← Telegram alerts
├── data/
│   ├── *.parquet           ← Cache dữ liệu
│   └── pipeline.py         ← Fetch/cache/LiveTickStream
├── docs/
│   ├── technical_guide.md  ← Tài liệu kỹ thuật (tài liệu này)
│   ├── user_guide.md       ← Hướng dẫn sử dụng
│   └── service_handbook.md ← Sổ tay vận hành
├── logs/
│   ├── live_state.json     ← Trạng thái live
│   ├── signal_server.log   ← Log giao dịch
│   └── worker_status.json  ← Trạng thái workers
├── main.py                 ← CLI entry point
├── mt5_bridge/
│   ├── ATS_Panel.mq5           ← EA MQL5 (đặt lệnh thật)
│   ├── ATS_StrategyView.mq5    ← Indicator MQL5 (visualize chiến lược)
│   ├── multi_runner.py         ← Orchestrator đa symbol
│   └── signal_server.py        ← ZMQ publisher
├── risk/
│   ├── kelly.py            ← Fractional Kelly
│   └── kill_switch.py      ← Ngắt khẩn cấp
├── start.py                ← Startup tất cả components
└── tests/
    └── test_ats.py         ← 38 test cases
```
