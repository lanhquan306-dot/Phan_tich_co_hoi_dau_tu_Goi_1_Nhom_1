# FinTech Copilot — Chatbot Telegram phân tích cổ phiếu Việt Nam

Chatbot Telegram giúp nhà đầu tư cá nhân tra cứu và phân tích cổ phiếu niêm yết trên HOSE/HNX bằng tiếng Việt tự nhiên. Bot có ba phần chính:

1. **Rule engine** tự tính giá, chỉ báo kỹ thuật, chỉ số tài chính và ra tín hiệu **BUY / HOLD / SELL** theo một chiến lược chấm điểm 100 điểm (kết hợp CANSLIM, sức mạnh tương đối và breakout).
2. **Copilot hội thoại** hiểu câu hỏi tự do (kể cả câu nối tiếp như "tại sao?", "so với VIC thì sao?"), trả lời bằng số liệu của engine và dùng LLM (Groq) để viết câu trả lời tự nhiên hơn.
3. **Biểu đồ nến** gồm MA20/MA50/MA150, khối lượng và RSI14, gửi dưới dạng ảnh và có thêm bản tương tác kiểu sàn giao dịch (rê chuột để xem chi tiết từng phiên).
4. **Backtest** chạy lại đúng bộ quy tắc của bot trên dữ liệu quá khứ (tín hiệu ngày t → khớp giá mở cửa t+1, có phí, thuế, T+2, giới hạn vị thế) và báo Total Return, Win Rate, Profit Factor, Max Drawdown, so sánh VN-Index. Chạy bằng nút 🧪 trên Telegram hoặc `python backtest.py`.

> Dự án phục vụ mục đích học tập và mô phỏng chiến lược. Kết quả **không phải tư vấn đầu tư**.

---

## Mục lục

1. [Tính năng](#1-tính-năng)
2. [Nguyên tắc thiết kế](#2-nguyên-tắc-thiết-kế)
3. [Kiến trúc tổng thể](#3-kiến-trúc-tổng-thể)
4. [Cấu trúc thư mục](#4-cấu-trúc-thư-mục)
5. [Luồng xử lý](#5-luồng-xử-lý)
6. [Mô tả chi tiết từng module](#6-mô-tả-chi-tiết-từng-module)
7. [Chiến lược đầu tư](#7-chiến-lược-đầu-tư)
8. [Công thức các chỉ báo](#8-công-thức-các-chỉ-báo)
9. [Tầng hội thoại và LLM](#9-tầng-hội-thoại-và-llm)
10. [Biểu đồ nến và dashboard tương tác](#10-biểu-đồ-nến-và-dashboard-tương-tác)
11. [Dữ liệu, cache và độ tin cậy](#11-dữ-liệu-cache-và-độ-tin-cậy)
12. [Cài đặt và chạy](#12-cài-đặt-và-chạy)
13. [Cấu hình](#13-cấu-hình)
14. [Lệnh Telegram](#14-lệnh-telegram)
15. [Kiểm thử](#15-kiểm-thử)
16. [Giới hạn và hướng phát triển](#16-giới-hạn-và-hướng-phát-triển)
17. [Backtest chiến lược](#17-backtest-chiến-lược)
18. [Kiểm định độ chính xác tín hiệu](#18-kiểm-định-độ-chính-xác-tín-hiệu-test-ở-quá-khứ)

---

## 1. Tính năng

| Nhóm | Người dùng làm gì | Bot trả về |
|---|---|---|
| Phân tích một mã | "FPT có nên mua không?" | Tín hiệu BUY/HOLD/SELL, điểm T1/T2/T3, nút thắt chính, điều cần theo dõi, bảng số liệu kiểm chứng |
| So sánh | "FPT với VCB mã nào phù hợp hơn?" | Đối chiếu từng tiêu chí; nếu không mã nào đạt BUY thì nói rõ, không ép chọn |
| Hỏi nối tiếp | "tại sao?", "T3 của nó vì sao bằng 0?" | Giải thích dựa trên kết quả vừa tính, không gọi lại nguồn dữ liệu |
| Giá | "giá HPG bao nhiêu?" | Giá, % thay đổi, O/H/L, khối lượng |
| Thị trường | "thị trường hôm nay thế nào?" | Trạng thái VN-Index (UPTREND / PRESSURE / WEAK / DOWNTREND) |
| Biểu đồ | "biểu đồ FPT", "chart HPG 1 năm" | Ảnh nến + MA + Volume + RSI, nút đổi khung thời gian, nút mở biểu đồ tương tác |
| Theo dõi | `/watch FPT` | Báo khi tín hiệu của mã thay đổi |
| Vị thế mô phỏng | `/position add FPT 100000` | Engine xét thêm điều kiện bán (cắt lỗ, trailing stop...) |
| Quét danh sách | `/top` | Xếp hạng các mã trong `TOP_SYMBOLS` |
| Backtest | Nút 🧪, `/backtest 6M`, "backtest FPT HPG" | Ảnh Equity Curve / Drawdown so với VN-Index, thẻ Performance Summary, Trade Log CSV |

---

## 2. Nguyên tắc thiết kế

1. **Engine là nguồn sự thật duy nhất.** Giá, chỉ báo, điểm và tín hiệu đều do code Python tính theo quy tắc cố định. LLM chỉ được diễn đạt lại, không được tính lại hay đổi tín hiệu. Cách này tránh việc LLM "bịa" số liệu.
2. **Vẫn chạy được khi không có LLM.** Mọi luồng về cổ phiếu (giá, phân tích, so sánh, giải thích, thị trường, biểu đồ) đều có câu trả lời soạn theo quy tắc. Groq chỉ là lớp tăng cường; khi Groq lỗi hoặc hết quota, bot vẫn trả lời đầy đủ.
3. **Hiểu ý định trước khi gọi API.** Câu hỏi được phân loại bằng từ khóa trước. Nhờ vậy "giá FPT?" chỉ lấy giá, không chạy cả chiến lược tốn nhiều lượt gọi dữ liệu.
4. **Không làm sập bot vì lỗi dữ liệu.** Mỗi nguồn con (lịch sử giá, VN-Index, BCTC, bảng giá) lỗi thì được ghi vào `warnings` / `errors` và phần còn lại vẫn tính tiếp. Lỗi vượt quota VNStock được đổi thành thông báo "thử lại sau 1 phút".
5. **Minh bạch.** Mỗi câu trả lời kèm checklist 6 điều kiện BUY, điểm từng tầng, bản đồ mốc giá, ngày dữ liệu và nguồn, để người dùng tự đối chiếu.
6. **Tách lớp rõ ràng.** Mỗi file một trách nhiệm. `strategy.py` là hàm thuần (không gọi mạng) nên kiểm thử được hoàn toàn offline.

---

## 3. Kiến trúc tổng thể

```text
                         ┌──────────────────────────────┐
     Người dùng  ──────▶ │  Telegram                     │
                         └──────────────┬───────────────┘
                                        │ tin nhắn / lệnh / nút bấm
                                        ▼
┌────────────────────────────────────────────────────────────────────────┐
│ bot.py            Nhận tin, định tuyến, gửi kết quả, quản lý bộ nhớ chat │
└───────┬───────────────────────┬──────────────────────────┬─────────────┘
        │ chat tự nhiên          │ lệnh /signal /quote...   │ "biểu đồ FPT"
        ▼                        │                          ▼
┌───────────────────┐            │              ┌────────────────────────┐
│ llm.py            │            │              │ chart_service.py       │
│ điều phối lượt chat│            │              │ PNG + trang HTML       │
│ + Groq (tùy chọn) │            │              └──────────┬─────────────┘
└───┬───────────┬───┘            │                         │
    │           │ hiểu ý định     │                         │  dashboard_server.py
    │           ▼                 │                         │  (web /chart/<MÃ>)
    │   ┌──────────────────┐      │                         │
    │   │ conversation.py  │      │                         │
    │   │ ý định + câu trả │      │                         │
    │   │ lời theo quy tắc │      │                         │
    │   └──────────────────┘      │                         │
    ▼                             ▼                         ▼
┌────────────────────────────────────────────────────────────────────────┐
│ stock_service.py   Tầng dữ liệu: rate limit, cache, chuẩn hóa, BCTC,    │
│                    analyze_symbol()                                     │
└───────┬─────────────────────────────────────────────┬──────────────────┘
        │ gọi dữ liệu                                  │ chấm điểm
        ▼                                              ▼
┌──────────────────────┐                   ┌──────────────────────────────┐
│ source_adapter.py    │                   │ strategy.py                  │
│ VCI → MAS → KBS      │                   │ chỉ báo, T1/T2/T3, bộ lọc,   │
│ (thư viện vnstock)   │                   │ cổng thị trường, BUY/HOLD/SELL│
└──────────────────────┘                   └──────────────────────────────┘

presentation.py : biến kết quả thành tin nhắn HTML của Telegram
config.py       : đọc cấu hình .env          utils.py : định dạng số, xử lý chữ
```

**Chiều phụ thuộc** chỉ đi từ trên xuống: `strategy.py` không biết Telegram hay VNStock tồn tại; `stock_service.py` không biết LLM tồn tại.

---

## 4. Cấu trúc thư mục

```text
fintech_copilot/
├── bot.py                 Điểm chạy chính. Handler Telegram cho lệnh, nút bấm, chat tự nhiên
├── llm.py                 Điều phối một lượt chat; gọi Groq (làm mượt câu trả lời, tool calling)
├── conversation.py        Phân loại ý định, suy ra mã từ ngữ cảnh, soạn câu trả lời theo quy tắc
├── presentation.py        Định dạng kết quả thành tin nhắn Telegram (HTML)
├── stock_service.py       Tầng dữ liệu: rate limit, cache, danh sách mã, giá, BCTC, analyze_symbol
├── source_adapter.py      Gọi thư viện vnstock với thứ tự nguồn và cơ chế dự phòng
├── strategy.py            Chiến lược: chỉ báo, chấm điểm, bộ lọc cứng, cổng thị trường, quyết định
├── chart_service.py       Biểu đồ nến: ảnh PNG (matplotlib) và trang HTML tương tác
├── dashboard_server.py    Web server nhỏ phục vụ trang biểu đồ tương tác
├── backtest.py            Backtest: dữ liệu lịch sử, Replay pipeline, mô phỏng t → t+1, danh mục, Metrics, báo cáo
├── signal_validation.py   Kiểm định độ chính xác: chạy lại bot từng phiên quá khứ, chấm ĐÚNG/SAI bằng giá tương lai
├── config.py              Đọc cấu hình từ .env, gồm toàn bộ ngưỡng chiến lược
├── utils.py               Hàm dùng chung: ép kiểu số, định dạng số, bỏ dấu tiếng Việt
├── check_groq.py          Kiểm tra nhanh API key / model Groq
├── selftests.py           22 nhóm kiểm thử offline (không cần mạng, không cần token)
├── data/                  (tự tạo) backtest_cache/ và backtest_reports/ — đã có trong .gitignore
├── templates/
│   └── dashboard.html     Giao diện trang biểu đồ tương tác (HTML + CSS + JavaScript)
├── static/
│   ├── lightweight-charts.standalone.production.js   Thư viện biểu đồ TradingView (v5.0.9)
│   └── LIGHTWEIGHT_CHARTS_LICENSE.txt                 Giấy phép Apache-2.0 của thư viện
├── requirements.txt       Thư viện Python cần cài
├── .env.example           Mẫu cấu hình, có chú thích từng biến
└── .gitignore
```

---

## 5. Luồng xử lý

### 5.1. Một câu chat tự nhiên — ví dụ "OCB có nên mua không?"

```text
1. bot.natural_message
   ├─ Là lời chào?            → trả lời chào, dừng
   ├─ Là yêu cầu biểu đồ?     → luồng biểu đồ (mục 5.3), dừng
   └─ Còn lại                 → llm.run_financial_agent(câu hỏi, lịch sử, bộ nhớ phân tích, vị thế, mã gần đây)

2. llm.run_financial_agent
   ├─ stock_service.resolve_symbols   → tìm mã trong câu: ["OCB"]
   ├─ conversation.classify_intent    → ý định: "analysis"
   ├─ conversation.contextual_symbols → bổ sung mã từ ngữ cảnh nếu câu dùng "nó", "so với..."
   └─ _stock_turn:
        ├─ _analyze_many            → stock_service.analyze_symbol("OCB") (song song nếu nhiều mã)
        ├─ conversation.single_analysis_summary → bản trả lời chuẩn soạn theo quy tắc
        └─ _polish                   → nhờ Groq viết lại tự nhiên hơn (tối đa 1 lần gọi);
                                       Groq lỗi hoặc viết chung chung → giữ bản chuẩn

3. stock_service.analyze_symbol("OCB")
   ├─ listing_map          → sàn, tên công ty
   ├─ get_history          → OHLCV ~620 ngày (có cache 15 phút)
   ├─ live_or_history_quote→ giá hiện tại (trong giờ giao dịch) hoặc giá nến cuối (ngoài giờ)
   ├─ get_market_gate      → VN-Index + trạng thái thị trường
   ├─ strategy.compute_technical → MA, RSI, ROC, breakout, khối lượng...
   ├─ fundamental_snapshot → EPS YoY, ROE TTM, nợ vay/VCSH
   └─ strategy.evaluate    → điểm T1/T2/T3, bộ lọc cứng, BUY/HOLD/SELL + lý do

4. bot.natural_message (tiếp)
   ├─ Lưu kết quả vào analysis_memory (5 mã gần nhất) để trả lời câu nối tiếp
   ├─ presentation.conversational_stock_card → tin nhắn HTML
   └─ Gửi kèm nút [📈 Biểu đồ] [📋 Chi tiết] [🔔 Theo dõi]
```

Câu tiếp theo "**tại sao?**" được phân loại là `explain`, mã được suy ra là OCB từ ngữ cảnh. Bot dùng lại kết quả trong `analysis_memory` nên **không gọi VNStock lần nào**.

### 5.2. Lệnh `/signal FPT`

`resolve_symbol` kiểm tra mã (gõ sai thì hiện nút gợi ý mã gần giống) → `analyze_symbol` → `presentation.signal_card` in toàn bộ chi tiết chấm điểm (từng thành phần, bộ lọc cứng, lý do, cảnh báo dữ liệu) → nếu có Groq thì gửi thêm một đoạn giải thích (`llm.explain_stock`). Lệnh này dùng để kiểm tra/đối chiếu engine.

### 5.3. Biểu đồ — "biểu đồ FPT 3 tháng"

```text
chart_service.is_chart_request     → đúng là yêu cầu biểu đồ
stock_service.resolve_symbols      → "FPT" (không có mã thì dùng mã vừa nói tới)
chart_service.period_from_text     → "3M"
chart_service.build_chart_frame    → get_history + compute_indicators (MA20/50/150, RSI14, MA20 khối lượng)
chart_service.render_chart_png     → ảnh 1800×1140 px
chart_service.chart_caption        → chú thích: OHLC, KL, GTGD, MA, RSI
bot.send_chart                     → gửi ảnh + 2 hàng nút:
     [1T] [3T] [6T] [1N]           → vẽ lại, thay ảnh tại chỗ (không gửi tin mới)
     [🔍 Mở biểu đồ tương tác]      → mở web dashboard hoặc gửi file HTML (xem mục 10)
```

---

## 6. Mô tả chi tiết từng module

### 6.1. `bot.py` — giao tiếp Telegram

Dùng thư viện `python-telegram-bot` (bất đồng bộ). Mọi hàm tính toán nặng (gọi dữ liệu, vẽ biểu đồ) được chạy trong thread riêng bằng `asyncio.to_thread` để bot không bị "đứng" khi chờ.

| Hàm | Vai trò |
|---|---|
| `main` | Đăng ký lệnh và nút bấm, bật job kiểm tra watchlist định kỳ, bật dashboard server, chạy bot |
| `natural_message` | Xử lý mọi tin nhắn không phải lệnh: chào hỏi, biểu đồ, hoặc chuyển cho `run_financial_agent` rồi chọn thẻ hiển thị theo `reply.mode` |
| `send_signal` | Chạy `analyze_symbol` và gửi thẻ `/signal` (dùng cho cả lệnh và nút gợi ý mã) |
| `send_chart` | Vẽ biểu đồ và gửi ảnh; nếu người dùng bấm nút đổi khung trên chính ảnh đó thì thay ảnh tại chỗ bằng `edit_message_media` |
| `send_chart_file` | Gửi trang biểu đồ tương tác dưới dạng file `.html` |
| `chart_keyboard` | Tạo nút dưới ảnh; chọn giữa nút Web App, nút link hay nút gửi file (mục 10.3) |
| `_resolve_arg` | Nhận diện mã từ tham số lệnh; sai mã thì gửi nút gợi ý |
| `_remember`, `_remember_symbols` | Bộ nhớ ngắn hạn: 8 lượt hội thoại, 5 mã gần nhất |
| `monitor_watchlist` | Chạy mỗi `ALERT_SECONDS` giây, báo khi tín hiệu của mã trong watchlist đổi |

**Trạng thái** (watchlist, vị thế, bộ nhớ hội thoại) lưu trong RAM, khởi động lại bot là mất.

### 6.2. `llm.py` — điều phối lượt chat và Groq

| Thành phần | Vai trò |
|---|---|
| `run_financial_agent` | Điểm vào chính. Xác định ý định và mã, rồi chuyển tới một trong các luồng bên dưới |
| `_stock_turn` | Phân tích một mã hoặc so sánh nhiều mã |
| `_explain_turn` | Câu hỏi nối tiếp; ưu tiên dùng lại kết quả cũ (`should_reuse_previous`) |
| `_quote_turn`, `_market_turn` | Chỉ lấy giá / chỉ lấy trạng thái thị trường |
| `_polish` | Gửi bản trả lời chuẩn + dữ liệu engine (JSON) cho Groq, yêu cầu viết lại mà không đổi số liệu. Kết quả phải qua bộ lọc chất lượng |
| `_open_question_turn` | Câu hỏi tài chính mở: Groq tự quyết định gọi công cụ nào (tool calling) |
| `_EngineTools` | 4 công cụ Groq được phép gọi: `analyze_stocks`, `get_stock_quotes`, `get_market_snapshot`, `get_strategy_rules`. Kết quả đầy đủ được giữ lại để bot vẫn hiển thị thẻ số liệu |
| `build_stock_context` | Rút gọn kết quả `analyze_symbol` thành JSON gửi cho Groq; chỉ chứa output của engine |
| `_record_groq_error` | "Cầu dao" (circuit breaker): sai key / bị từ chối thì ngắt Groq 10 phút, hết quota thì ngắt 90 giây |
| `explain_stock` | Đoạn giải thích AI gửi kèm lệnh `/signal` |
| `AgentReply` | Kết quả một lượt: câu trả lời, chế độ hiển thị, dữ liệu engine, dấu vết các bước (`tool_trace`), lỗi |

### 6.3. `conversation.py` — hiểu câu hỏi và soạn câu trả lời theo quy tắc

Không gọi mạng, không gọi LLM. Câu hỏi được bỏ dấu (`utils.normalize_text`) rồi so khớp với các bộ từ khóa.

| Hàm | Vai trò |
|---|---|
| `classify_intent` | Trả về một trong 7 ý định (bảng ở mục 9.1) |
| `contextual_symbols` | Bổ sung mã từ ngữ cảnh: "so với VIC" sau khi hỏi OCB thành `[OCB, VIC]`; "tại sao?" thành `[OCB]` |
| `should_reuse_previous` | Dùng lại kết quả cũ trừ khi câu hỏi có "hôm nay", "hiện tại", "mới nhất"... |
| `single_analysis_summary` | Kết luận, cấu trúc điểm, nút thắt chính, động lượng, cơ bản, điều cần theo dõi |
| `explanation_summary` | Trả lời "tại sao", giải thích riêng một tầng (T1/T2/T3) hoặc bộ lọc cứng |
| `comparison_summary` | So sánh nhiều mã theo thứ tự tín hiệu, điểm, Delta ROC63 |
| `quote_summary`, `market_summary`, `strategy_summary` | Câu trả lời cho giá, thị trường, quy tắc chiến lược |
| `score_text`, `tier_score_text`, `tiers_line` | Hiển thị điểm; khi thiếu dữ liệu T2.1 thì hiện khoảng, ví dụ `62–77` |
| `ai_response_is_useful` | Bộ lọc chất lượng cho câu trả lời của Groq (mục 9.3) |

### 6.4. `presentation.py` — giao diện tin nhắn

Chuyển kết quả thành HTML theo chuẩn Telegram. Mọi chữ lấy từ dữ liệu hoặc LLM đều được `html.escape` để tránh vỡ định dạng. Mỗi thẻ phân tích có cùng bố cục:

```text
🟡 FPT · HOLD (CHỜ)                          ← tiêu đề: icon tín hiệu, mã, loại HOLD
Công ty Cổ phần FPT · HOSE
━━━━━━━━━━━━━━━━━━
💰 113,306 🔺 +735 (+0.65%) · 💵 474.5 tỷ    ← giá, thay đổi, giá trị giao dịch
🎯 Điểm 63/100 ▰▰▰▰▰▰▱▱▱▱ · BUY ≥ 60          ← thanh tiến độ + ngưỡng + số điểm còn thiếu
🏛 Thị trường 🟢 UPTREND — được mở mua mới
━━━━━━━━━━━━━━━━━━
💬 Câu trả lời (engine soạn, Groq làm mượt)
📋 Điều kiện BUY: 5/6                         ← checklist 6 điều kiện bắt buộc
✅ Thị trường  ✅ Bộ lọc  ✅ BCTC
❌ Breakout  ✅ Đủ điểm  ✅ Chưa giữ
┃ 🔎 Chi tiết chấm điểm   (khối thu gọn, bấm để mở)
┃ 🧱 T1 / 📈 T2 / ⚡ T3 kèm từng thành phần điểm
┃ 🧭 Xu hướng · ⚡ Động lượng · 🏁 Sức mạnh · 🧾 Cơ bản
┃ 🗺 Bản đồ mốc giá (đỉnh 52 tuần, vùng mua, pivot, MA, đáy 20 phiên, cắt lỗ)
🕒 EOD · VN-Index · BCTC · nguồn
[📈 Biểu đồ] [📋 Chi tiết] [🔔 Theo dõi]      ← nút bấm
```

Một số quy ước giao diện:

- **Icon tín hiệu:** 🟢 BUY, 🟡 HOLD, 🔴 SELL.
- **Icon thị trường:** 🟢 UPTREND, 🟠 PRESSURE, 🟡 WEAK, 🔴 DOWNTREND.
- **Icon MA:** 🟨 MA20, 🟦 MA50, 🟪 MA150, trùng màu các đường trên ảnh biểu đồ.
- **RSI:** 🔥 quá mua (≥ 70), 🧊 quá bán (≤ 30), ⚖️ trung tính.
- **Khối thu gọn** (`<blockquote expandable>`): phần chi tiết dài chỉ hiện vài dòng đầu, người dùng bấm để mở. Nhờ vậy thẻ vừa gọn vừa đủ thông tin.
- **Bảng so sánh** dùng thẻ `<pre>` (chữ đơn cách) để các cột số thẳng hàng.
- **Quy tắc HTML của Telegram:** trong blockquote chỉ được dùng in đậm/nghiêng; `<pre>` không được nằm trong blockquote. Kiểm thử `test_ui_cards` và `test_cards_render` tự kiểm tra các quy tắc này và giới hạn 4096 ký tự.

| Hàm | Dùng cho | Thông tin chính |
|---|---|---|
| `conversational_stock_card` | Chat về một mã | Giá, điểm, thị trường, câu trả lời, checklist BUY, chi tiết thu gọn |
| `signal_card` | Lệnh `/signal`, nút 📋 | Như trên + kế hoạch vào lệnh (khi BUY), tỷ trọng, bộ lọc cứng từng điều kiện, lý do, cảnh báo dữ liệu |
| `comparison_card` | Chat so sánh | Kết luận 🏆, thanh điểm từng mã, bảng 15 chỉ tiêu đặt cạnh nhau |
| `quote_card`, `multi_quote_card` | Giá | Giá, thay đổi, vị trí giá trong biên độ phiên (━━●━━), mở cửa, tham chiếu, KL, GTGD |
| `market_card`, `market_snapshot_card` | Thị trường | Trạng thái, VN-Index so với MA50, phiên phân phối dạng ●●●○, ngưỡng BUY, tỷ trọng, giải thích 4 trạng thái |
| `top_card` | Lệnh `/top` | Xếp hạng 🥇🥈🥉, thanh điểm, T1/T2/T3, giá, ΔROC63 |
| `positions_card` | Lệnh `/positions` | Lãi/lỗ tạm tính, giá cắt lỗ, đỉnh đã đạt, trailing stop |
| `watchlist_card` | Lệnh `/watchlist` | Tín hiệu gần nhất của từng mã theo dõi |
| `price_map` | Trong các thẻ phân tích | Các mốc giá xếp từ cao xuống thấp, chèn giá hiện tại vào giữa, kèm % khoảng cách |

Các **nút bấm** dưới thẻ do `bot.actions_keyboard` tạo: `📈 Biểu đồ` (gửi ảnh nến), `📋 Chi tiết` (gửi thẻ `/signal` thành tin mới), `🔔 Theo dõi` (thêm vào watchlist, hiện thông báo nhỏ).

### 6.5. `stock_service.py` — tầng dữ liệu

| Phần | Nội dung |
|---|---|
| Giới hạn tần suất | `RateLimiter`: cửa sổ trượt 60 giây, mặc định 16 lần/phút (50 nếu có API key VNStock). `vn_call` bọc mọi lần gọi VNStock; bắt cả `SystemExit` mà VNStock ném ra khi vượt quota |
| Cache | `TTLCache` trong RAM, an toàn đa luồng. `cached_call` trả về `(giá trị, lấy từ cache, là cache hết hạn)`; nguồn lỗi thì dùng tạm cache cũ thay vì báo lỗi |
| Chuẩn hóa | `normalize_ohlcv` đưa dữ liệu mọi nguồn về 6 cột `time/open/high/low/close/volume`; `normalize_equity_price` đổi giá tính theo nghìn đồng (71.7) sang VND (71,700) |
| Danh sách mã | `listing_map` (cache 24 giờ), `resolve_symbols` (nhiều mã trong câu chat, chỉ nhận mã có niêm yết), `resolve_symbol` (một mã từ lệnh, gợi ý mã gần giống bằng `difflib`) |
| Giá | `get_history`, `get_index_history`, `get_quote`, `live_or_history_quote`, `confirmed_bars` (bỏ nến của phiên đang chạy) |
| BCTC | `fundamental_snapshot`: đọc báo cáo quý từ VCI (cần ≥ 5 quý), thiếu thì dùng KBS. Tìm dòng chỉ tiêu theo tên (`_find_report_row`) và nhận diện cột quý ở nhiều định dạng (`_parse_quarter`) |
| Phân tích | `analyze_symbol` ghép tất cả và gọi `strategy.evaluate` |

### 6.6. `source_adapter.py` — kết nối VNStock

Thư viện `vnstock` 4.x đặt các class `Quote`, `Trading`, `Listing` ở những module khác nhau tùy phiên bản, và nhận tham số khởi tạo khác nhau. Module này dò lần lượt các vị trí (`_find_class`) và các bộ tham số (`_instantiate`), rồi thử các nguồn theo thứ tự cấu hình (`_try_sources`), mặc định **VCI → MAS → KBS**. MAS chỉ dùng được khi cài gói trả phí `vnstock_data`.

### 6.7. `strategy.py` — chiến lược

Toàn bộ là hàm thuần: nhận DataFrame/dict, trả dict, không gọi mạng. Chi tiết quy tắc ở [mục 7](#7-chiến-lược-đầu-tư).

| Hàm | Vai trò |
|---|---|
| `compute_technical` | Tính mọi chỉ báo trên nến cuối |
| `market_gate`, `distribution_days` | Trạng thái thị trường từ VN-Index |
| `score_tier1`, `score_tier2`, `score_t21`, `score_tier3` | Chấm từng tầng |
| `hard_filter` | 5 điều kiện bắt buộc |
| `exit_reason`, `avoid_reason` | Điều kiện bán khi có vị thế / điều kiện tránh mua |
| `evaluate` | Ra quyết định cuối cùng kèm danh sách lý do |

### 6.8. `chart_service.py` và `dashboard_server.py` — biểu đồ

Xem [mục 10](#10-biểu-đồ-nến-và-dashboard-tương-tác).

### 6.9. `config.py` và `utils.py`

- `config.py` đọc `.env` bằng `python-dotenv`. Mọi ngưỡng của chiến lược nằm trong dataclass bất biến `StrategyConfig`; các hàm của `strategy.py` nhận tham số `cfg` để kiểm thử có thể truyền ngưỡng khác.
- `utils.py` gom các hàm trước đây bị viết lặp ở nhiều file: `to_float` (ép kiểu an toàn, NaN/None thành `None`), `fmt_price`, `fmt_pct`, `fmt_money`, `fmt_volume`, `normalize_text` (bỏ dấu tiếng Việt, xử lý riêng chữ "đ"), `padded`.

---

## 7. Chiến lược đầu tư

Chiến lược chấm **100 điểm** chia ba tầng, kết hợp với **cổng thị trường** và **bộ lọc cứng**. Mọi phép tính dùng **nến ngày đã đóng cửa** (EOD); trong giờ giao dịch, nến của phiên đang chạy bị bỏ qua.

### 7.1. Tầng 1 — Nền tảng cơ bản (30 điểm)

| Mã | Tiêu chí | Điểm |
|---|---|---|
| T1.1 | Tăng trưởng LNST cổ đông công ty mẹ quý gần nhất so với cùng kỳ (YoY) | ≥ 20% → **20**; ≥ 10% → **8**; còn lại, thiếu dữ liệu hoặc cùng kỳ ≤ 0 → 0 |
| T1.2 | ROE TTM (4 quý gần nhất) | ≥ 15% → **10**; ≥ 10% → **5**; còn lại → 0 |

Dùng tăng trưởng LNST cổ đông công ty mẹ thay cho EPS báo cáo, vì EPS bị méo khi doanh nghiệp chia cổ phiếu thưởng. Nếu BCTC không có dòng này thì mới dùng dòng EPS.

### 7.2. Tầng 2 — Xu hướng và sức mạnh (30 điểm)

| Mã | Tiêu chí | Điểm |
|---|---|---|
| T2.1 | Delta ROC63 = ROC63(mã) − ROC63(VN-Index), đơn vị điểm % | ≥ 15 → **15**; ≥ 5 → **8**; còn lại → 0 |
| T2.2 | Xu hướng giá | Close > MA50 **và** MA50 > MA150 → **10**; chỉ Close > MA50 → **5**; còn lại → 0 |
| T2.3 | Close / giá đóng cửa cao nhất 52 tuần (252 phiên) | ≥ 85% → **5**; ≥ 75% → **2**; còn lại → 0 |

Khi không tính được T2.1 (thiếu dữ liệu VN-Index), điểm T2.1 là **chưa biết** chứ không phải 0. Tổng điểm khi đó là một khoảng `[score_min, score_max]` với độ rộng 15 điểm, và bot chỉ BUY nếu ngay cả **điểm tối thiểu** đã đạt ngưỡng.

### 7.3. Tầng 3 — Breakout (40 điểm)

| Mã | Tiêu chí | Điểm |
|---|---|---|
| T3.1 | Breakout còn hiệu lực (**bắt buộc để BUY**) | Breakout ≤ 2 phiên trước → **25**; 3–9 phiên → **15**; không có hoặc hết hiệu lực → 0 |
| T3.2 | KL trung bình 3 phiên gần nhất / KL trung bình 20 phiên trước đó | ≥ 1.5 lần → **10**; ≥ 1.2 lần → **5**; còn lại → 0 |
| T3.3 | RSI14 nằm trong khoảng [50, 85] | **5** |

Breakout "còn hiệu lực" khi đồng thời thỏa ba điều kiện:

1. Có phiên breakout trong 10 phiên gần nhất.
2. Giá đóng cửa hiện tại vẫn **trên** pivot của phiên breakout.
3. Giá đóng cửa hiện tại chưa vượt quá pivot × 1.07 (**vùng mua**, tránh mua đuổi).

### 7.4. Cổng thị trường (VN-Index)

| Trạng thái | Điều kiện | Ngưỡng BUY | Tỷ trọng mỗi mã |
|---|---|---|---|
| UPTREND | Close > MA50 và < 4 phiên phân phối trong 25 phiên | ≥ **60** điểm | 10% NAV |
| PRESSURE | Close > MA50 và ≥ 4 phiên phân phối (hoặc thiếu dữ liệu khối lượng để đếm) | ≥ **75** điểm | 5% NAV (giảm một nửa) |
| WEAK | Close ≤ MA50 trong một phiên | Không mở BUY mới | 0 |
| DOWNTREND | Close < MA50 hai phiên liên tiếp | Không mở BUY mới | 0 |

**Phiên phân phối** (theo William O'Neil): VN-Index giảm ≥ 0.2% so với phiên trước **và** khối lượng lớn hơn phiên trước. Đây là dấu hiệu tổ chức đang bán ra.

### 7.5. Bộ lọc cứng

Phải đạt **cả 5** điều kiện, dù điểm cao đến đâu:

| Điều kiện | Yêu cầu |
|---|---|
| Sàn | HOSE hoặc HNX |
| Trạng thái | Không nằm trong `BLOCKED_SYMBOLS` |
| Thanh khoản | Giá trị giao dịch trung bình 20 phiên ≥ 10 tỷ đồng |
| Lịch sử | ≥ 253 phiên giao dịch |
| Nợ | Nợ vay chịu lãi / VCSH ≤ 1.5 (nhóm ngân hàng, chứng khoán, bảo hiểm được miễn) |

### 7.6. Ra quyết định (`strategy.evaluate`)

Xét theo thứ tự, gặp điều kiện nào trước thì dừng:

```text
1. Đang có vị thế và chạm điều kiện bán        → SELL
2. Đang có vị thế                               → HOLD (NẮM GIỮ)
3. Chưa có vị thế nhưng xu hướng xấu            → HOLD (TRÁNH MUA)
       (Close < MA50 và MA50 dốc xuống, hoặc momentum breakdown)
4. Đạt TẤT CẢ điều kiện BUY:                    → BUY
       - cổng thị trường cho phép mua
       - bộ lọc cứng đạt
       - có dữ liệu BCTC
       - breakout còn hiệu lực (T3.1 > 0)
       - điểm tối thiểu ≥ ngưỡng của trạng thái thị trường
       - chưa có vị thế
5. Còn lại                                      → HOLD (CHỜ, hoặc CHỜ DỮ LIỆU nếu chỉ vướng T2.1 chưa biết)
```

Khi ra BUY, bot kèm **kỷ luật vào lệnh** cho phiên kế tiếp: chỉ mua khi giá mở cửa > pivot và ≤ vùng mua. Bot cũng đưa ra **giá cắt lỗ tham chiếu** bằng giá × (1 − 7%).

### 7.7. Điều kiện bán khi có vị thế (`strategy.exit_reason`)

| Điều kiện | Quy tắc |
|---|---|
| Cắt lỗ | Close ≤ giá mua × (1 − 7%) |
| Trailing stop | Đã từng lãi ≥ 15% và Close giảm ≥ 10% từ giá đóng cửa cao nhất kể từ khi mua |
| Breakdown | Close < đáy 20 phiên, KL ≥ 1.3 lần trung bình 20 phiên và RSI14 < 45 |
| Gãy MA50 | Dưới MA50 hai phiên liên tiếp, hoặc một phiên với KL ≥ 1.5 lần |
| Thị trường xấu | VN-Index DOWNTREND và Close < MA20 |

### 7.8. Ví dụ tính điểm

Số liệu này được lấy đúng từ kiểm thử `test_evaluate_by_market`.

| Thành phần | Dữ liệu | Điểm |
|---|---|---|
| T1.1 | LNST mẹ YoY +25% | 20 |
| T1.2 | ROE TTM 18% | 10 |
| T2.1 | ROC63 mã +25%, VN-Index +5% → Delta = 20 | 15 |
| T2.2 | Close 105 > MA50 100 > MA150 90 | 10 |
| T2.3 | Close / đỉnh 52 tuần = 90% | 5 |
| T3.1 | Breakout 1 phiên trước, giá trong vùng mua (100; 107] | 25 |
| T3.2 | KL 3 phiên / nền 20 phiên = 1.6 lần | 10 |
| T3.3 | RSI14 = 60 | 5 |
| **Tổng** | | **100** |

- Thị trường **UPTREND**: 100 ≥ 60 → **BUY**, tỷ trọng 10%.
- Thị trường **PRESSURE**: 100 ≥ 75 → **BUY**, tỷ trọng 5%.
- Cùng mã nhưng LNST cùng kỳ ≤ 0 và ROE 0% thì T1 = 0, tổng **70** điểm: vẫn BUY khi UPTREND (70 ≥ 60) nhưng chỉ **HOLD** khi PRESSURE (70 < 75).
- Thị trường **WEAK**: HOLD dù đủ 100 điểm.
- Không có breakout: HOLD dù các tầng khác đủ điểm.

---

## 8. Công thức các chỉ báo

| Chỉ báo | Công thức | Ghi chú |
|---|---|---|
| MA*n* | Trung bình cộng giá đóng cửa *n* phiên | MA20, MA50, MA150 |
| MA50 dốc xuống | MA50 hôm nay < MA50 của 10 phiên trước | |
| RSI14 (Wilder) | `RS = TB_lãi / TB_lỗ`, `RSI = 100 − 100 / (1 + RS)` | Giá trị đầu là trung bình cộng 14 phiên; sau đó `TB = (13 × TB_cũ + giá trị mới) / 14` (cùng cách TradingView) |
| ROC*n* | `(Close_t − Close_{t−n}) / Close_{t−n} × 100` | *n* = 63 phiên ≈ 3 tháng |
| Delta ROC63 | `ROC63(mã) − ROC63(VN-Index)` | VN-Index được cắt đến đúng ngày nến cuối của mã để hai ROC cùng mốc thời gian |
| Pivot | Giá đóng cửa cao nhất của 30 phiên **trước** phiên đang xét | |
| Phiên breakout | Phiên **đầu tiên** của một chuỗi phiên có Close > pivot | Nhờ vậy giá lập đỉnh mới mỗi ngày không làm "tuổi" breakout bị đếm lại từ 0; pivot được "đóng băng" tại phiên breakout |
| KL 3 phiên / nền 20 | `TB(KL phiên t, t−1, t−2) / TB(KL phiên t−3 … t−22)` | Hai cửa sổ không chồng lên nhau |
| GTGD 20 phiên | `TB(Close × KL)` 20 phiên | Dùng cho bộ lọc thanh khoản |
| ROE TTM | `Tổng LNST 4 quý / TB VCSH cuối kỳ của 4 quý × 100` | Giá trị ngoài khoảng ±100% bị coi là dữ liệu lỗi |
| Nợ vay / VCSH | `(Vay ngắn hạn + Vay dài hạn) / VCSH` quý mới nhất | Chỉ tính nợ vay **chịu lãi**, không tính tổng nợ phải trả |

---

## 9. Tầng hội thoại và LLM

### 9.1. Phân loại ý định

| Ý định | Ví dụ | Xử lý | Gọi VNStock? |
|---|---|---|---|
| `quote` | "giá FPT bao nhiêu?", "giá nó?" | Chỉ lấy bảng giá | Có (1 lần) |
| `analysis` | "FPT có nên mua không?", "cổ phiếu OCB" | Chạy toàn bộ chiến lược | Có |
| `compare` | "FPT với VCB mã nào tốt hơn?", "so với VIC thì sao?" | Phân tích song song các mã | Có |
| `explain` | "tại sao?", "T3 vì sao bằng 0?", "hard filter là gì?" | Dùng lại kết quả trong bộ nhớ | Không |
| `market` | "thị trường hôm nay thế nào?" | Trạng thái VN-Index | Có |
| `strategy` | "bot chấm điểm thế nào?" | Mô tả quy tắc | Không |
| `general` | "lãi suất tăng ảnh hưởng ngân hàng ra sao?" | Groq trả lời, tự gọi công cụ khi cần dữ liệu | Tùy Groq |

Thứ tự ưu tiên khi phân loại: so sánh → giá → thị trường → chiến lược → giải thích → phân tích. Câu "giá FPT có nên mua không?" có cả từ khóa giá lẫn từ khóa hành động, nên được xếp vào `analysis` vì ý định hành động mạnh hơn.

### 9.2. Bộ nhớ ngắn hạn

- `history`: 8 lượt hội thoại gần nhất, gửi kèm cho Groq để hiểu ngữ cảnh.
- `recent_symbols`: 5 mã được nhắc gần nhất, để hiểu "nó", "mã này".
- `analysis_memory`: kết quả `analyze_symbol` của 5 mã gần nhất. Bộ nhớ lưu **số liệu** chứ không lưu câu chữ, để câu nối tiếp dựa trên dữ liệu thật.

### 9.3. Làm mượt câu trả lời và bộ lọc chất lượng

Với các ý định về cổ phiếu, bot soạn **bản trả lời chuẩn** theo quy tắc trước. Sau đó bot gọi Groq **tối đa một lần**, gửi kèm bản nháp và dữ liệu engine dạng JSON, yêu cầu viết lại tự nhiên hơn mà không đổi bất kỳ số liệu nào. Câu trả lời của Groq bị **loại** (bot giữ bản chuẩn) nếu rơi vào một trong các trường hợp:

- Ngắn hơn 70 ký tự.
- Dưới 180 ký tự mà chứa câu chung chung kiểu "gửi báo cáo phân tích mã...".
- Không nhắc tới mã đang hỏi.
- Không có từ khóa quyết định nào (BUY, HOLD, SELL, /100, T1, T2, T3, breakout, MA50, ROC63).

### 9.4. Câu hỏi mở và tool calling

Chỉ khi câu hỏi không thuộc nhóm cổ phiếu, Groq mới được dùng làm "agent", theo giao thức tool calling tương thích OpenAI:

```text
Groq nhận: system prompt (quy tắc grounding) + lịch sử + câu hỏi + danh sách 4 công cụ
  ↓ Groq yêu cầu gọi công cụ, ví dụ analyze_stocks(["FPT", "VCB"])
Bot chạy công cụ trên engine thật → gửi kết quả JSON lại cho Groq
  ↓ lặp tối đa 4 vòng; quá 4 vòng thì buộc Groq trả lời bằng dữ liệu đã có
Groq viết câu trả lời cuối
```

System prompt cấm Groq tự tính lại hoặc đổi BUY/HOLD/SELL, cấm bịa tin tức hay số liệu, và yêu cầu nói rõ khi dữ liệu thiếu.

### 9.5. Cầu dao Groq (circuit breaker)

- Groq trả lỗi xác thực hoặc bị từ chối (401/403): ngắt **10 phút**.
- Groq báo hết quota (429): ngắt **90 giây**.

Trong thời gian ngắt, bot không gọi Groq nữa (tránh mỗi câu hỏi phải chờ timeout) và dùng bản trả lời chuẩn.

Groq được gọi qua API tương thích OpenAI (`https://api.groq.com/openai/v1`). Model mặc định `openai/gpt-oss-20b` là model suy luận, nên bot gửi `reasoning_effort=low` và `include_reasoning=false` để model không tiêu hết token cho phần suy luận ẩn trước khi viết câu trả lời.

---

## 10. Biểu đồ nến và dashboard tương tác

Phần này **chỉ trực quan hóa dữ liệu**, không chứa quy tắc BUY/SELL nào. RSI trên biểu đồ dùng chung hàm `strategy.wilder_rsi` với engine nên số liệu khớp nhau.

### 10.1. Ảnh gửi Telegram (`render_chart_png`)

Ảnh 1800 × 1140 px, nền tối kiểu TradingView, gồm 3 tầng:

1. **Giá**: nến Nhật (xanh = tăng, đỏ = giảm), MA20 (vàng), MA50 (xanh dương), MA150 (tím), nhãn giá đóng cửa cuối ở mép phải.
2. **Khối lượng**: cột theo màu nến, kèm đường trung bình 20 phiên (cam).
3. **RSI14**: đường kẻ ngưỡng 30 và 70; vùng trên 70 tô đỏ, vùng dưới 30 tô xanh.

Trục thời gian dùng số thứ tự phiên nên các ngày nghỉ không tạo khoảng trống. MA được tính trên **toàn bộ** lịch sử rồi mới cắt theo khung xem, nhờ đó MA150 có giá trị ngay từ nến đầu tiên của khung 6 tháng. Vì matplotlib không an toàn khi nhiều thread cùng vẽ, việc vẽ được khóa tuần tự (`_render_lock`).

Chú thích ảnh gồm: giá đóng cửa và thay đổi, O/H/L, khối lượng, GTGD, MA20/50/150, RSI14 và vùng RSI.

### 10.2. Trang tương tác (`render_dashboard_html` + `templates/dashboard.html`)

Trang dùng thư viện **TradingView Lightweight Charts v5** (giấy phép Apache-2.0). Thư viện và dữ liệu đều được nhúng thẳng vào một file HTML khoảng 240 KB, nên mở được cả khi không có mạng.

- **Rê chuột** (hoặc chạm giữ trên điện thoại) vào một nến sẽ hiện bảng chi tiết: thứ và ngày, mở/cao/thấp/đóng, thay đổi, biên độ, khối lượng so với trung bình 20 phiên, giá trị giao dịch, MA20/50/150 kèm % giá lệch khỏi từng MA, RSI14 kèm vùng.
- Dòng chú giải OHLC và MA ở góc trái cập nhật theo vị trí chuột.
- Nút khung thời gian 1T / 3T / 6T / 1N / Tất cả; bật/tắt từng đường MA; đổi nền sáng/tối; kéo và zoom.
- Hàng thống kê: cao/thấp 52 tuần, KL trung bình 20 phiên, KL phiên cuối so với trung bình, % giá so với từng MA, RSI hiện tại.
- Trên điện thoại, bảng chi tiết chuyển sang dạng 2 cột ở đầu biểu đồ để không che nến.

### 10.3. Nút "Mở biểu đồ tương tác"

Telegram không mở được link `localhost`, nên cách nút hoạt động phụ thuộc biến `DASHBOARD_PUBLIC_URL`:

| Cấu hình | Khi bấm nút |
|---|---|
| Để trống (mặc định) | Bot gửi file `.html`; mở bằng Chrome/Safari là tương tác được |
| Link `https://` và đang chat riêng với bot | Mở dạng **Web App** ngay trong Telegram |
| Link `https://` trong nhóm chat, hoặc link `http://` | Mở bằng trình duyệt |

Nếu Telegram từ chối link, bot tự quay về nút gửi file HTML. Khi có link, bên cạnh vẫn có nút "📄 File HTML".

### 10.4. Web server (`dashboard_server.py`)

Server nhỏ viết bằng `http.server` của thư viện chuẩn (không cần Flask), chạy trong thread nền cùng process với bot tại `http://127.0.0.1:8050`.

| Đường dẫn | Trả về |
|---|---|
| `/` | Trang nhập mã |
| `/chart/<MÃ>?p=6M` | Biểu đồ tương tác (`p` = 1M, 3M, 6M, 1Y, ALL) |
| `/healthz` | `ok` |

Server kiểm tra mã có niêm yết trước khi lấy dữ liệu (gõ sai thì hiện gợi ý) và cache mỗi trang 60 giây. Muốn mở từ Telegram, cần đưa server ra Internet, ví dụ bằng Cloudflare Tunnel:

```bash
cloudflared tunnel --url http://localhost:8050
# dán link https://xxxx.trycloudflare.com vào DASHBOARD_PUBLIC_URL rồi chạy lại bot
```

Lưu ý: ai có link cũng xem được biểu đồ (dữ liệu thị trường công khai). Mọi lượt lấy dữ liệu vẫn đi qua rate limiter của VNStock.

---

## 11. Dữ liệu, cache và độ tin cậy

| Dữ liệu | Nguồn (theo thứ tự) | Cache |
|---|---|---|
| Danh sách mã | VCI → vnstock Reference | 24 giờ |
| Lịch sử giá (~620 ngày lịch; thiếu phiên thì lấy lại 1500 ngày) | VCI → MAS → KBS | 15 phút |
| VN-Index | VCI → MAS → KBS | 15 phút |
| Bảng giá hiện tại | VCI → MAS → KBS → vnstock Market | 3 giây |
| Báo cáo tài chính quý | VCI (cần ≥ 5 quý) → KBS | 12 giờ |

Các cơ chế giúp dữ liệu đáng tin hơn:

- **Nến đã đóng.** Chiến lược luôn tính trên nến đã đóng cửa; ngoài giờ giao dịch, giá hiển thị lấy từ nến cuối nên không tốn thêm lượt gọi API.
- **Cache cũ khi nguồn lỗi.** Nguồn lỗi mà cache cũ còn thì dùng tạm, kèm cảnh báo "đang dùng cache cũ".
- **Kiểm tra giá trị bất thường.** ROE ngoài ±100% hoặc nợ vay/VCSH ngoài [0; 20] bị coi là dữ liệu lỗi.
- **Ghi rõ dữ liệu thiếu.** Thiếu dữ liệu được ghi vào `warnings` và hiện trong tin nhắn thay vì âm thầm coi là 0.

---

## 12. Cài đặt và chạy

Yêu cầu: **Python 3.10+** và một bot Telegram (tạo qua [@BotFather](https://t.me/BotFather) để lấy token).

```bash
# 1. Tạo môi trường ảo
python -m venv .venv
source .venv/bin/activate          # Windows: .\.venv\Scripts\Activate.ps1

# 2. Cài thư viện
pip install -U -r requirements.txt

# 3. Tạo file cấu hình rồi điền TELEGRAM_BOT_TOKEN (và GROQ_API_KEY nếu có)
cp .env.example .env               # Windows: Copy-Item .env.example .env

# 4. Chạy kiểm thử (phải ra ALL PASS)
python selftests.py

# 5. (Tùy chọn) kiểm tra Groq
python check_groq.py

# 6. Chạy bot
python bot.py
```

Thư viện sử dụng: `python-telegram-bot` (Telegram), `vnstock` (dữ liệu chứng khoán Việt Nam), `openai` (client gọi Groq), `pandas` / `numpy` (tính toán), `matplotlib` (vẽ ảnh), `python-dotenv` (đọc cấu hình).

---

## 13. Cấu hình

Mọi biến nằm trong `.env`; xem `.env.example` để có danh sách đầy đủ kèm chú thích. Các biến quan trọng:

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | (bắt buộc) | Token bot Telegram |
| `GROQ_API_KEY` | trống | Key Groq; để trống thì bot chạy không có LLM |
| `GROQ_MODEL` | `openai/gpt-oss-20b` | Model Groq |
| `LOCAL_VNSTOCK_BUDGET_PER_MIN` | 16 (50 nếu có key) | Số lần gọi VNStock tối đa mỗi phút |
| `PRICE_HISTORY_SOURCES` | `VCI,MAS,KBS` | Thứ tự nguồn lịch sử giá |
| `TOP_SYMBOLS` | `FPT,VNM,HPG` | Danh sách mã cho `/top` |
| `ALERT_SECONDS` | 600 | Chu kỳ kiểm tra watchlist (giây) |
| `CHART_DEFAULT_PERIOD` | `6M` | Khung mặc định của ảnh biểu đồ |
| `DASHBOARD_PORT` | 8050 | Cổng web dashboard |
| `DASHBOARD_PUBLIC_URL` | trống | Link public cho nút mở biểu đồ |
| `BUY_SCORE_UPTREND` / `BUY_SCORE_PRESSURE` | 60 / 75 | Ngưỡng BUY theo trạng thái thị trường |
| `MIN_ADV_VALUE` | 10 000 000 000 | GTGD trung bình 20 phiên tối thiểu (VND) |
| `BUY_ZONE` | 0.07 | Vùng mua tối đa trên pivot (7%) |
| `STOP` / `TRAIL_TRIGGER` / `TRAIL` | 0.07 / 0.15 / 0.10 | Cắt lỗ / mức lãi kích hoạt trailing / mức giảm từ đỉnh |
| `BACKTEST_START` / `BACKTEST_END` | trống | Khoảng backtest (YYYY-MM-DD). Trống: END = hôm nay, START = END − `BACKTEST_MONTHS` |
| `BACKTEST_MONTHS` | 6 | Số tháng mặc định khi không đặt `BACKTEST_START` |
| `BACKTEST_SYMBOLS` | 15 mã (FPT, HPG, MWG, VNM, VCB, MBB, TCB, SSI, HCM, VHM, VIC, GAS, PLX, GMD, PNJ) | Danh sách cố định |
| `INITIAL_CAPITAL` / `MAX_POSITIONS` / `USE_MARGIN` | 100 000 000 / 5 / false | Vốn, số vị thế tối đa, margin (chưa hỗ trợ) |
| `SELL_TAX` / `TRANSACTION_FEE` | 0.001 / 0.0015 | Thuế bán 0,1% / phí môi giới mỗi chiều |
| `BACKTEST_FUND_LAG_DAYS` | 45 | BCTC quý chỉ được dùng sau ngày cuối quý + số ngày này |
| `BACKTEST_SETTLEMENT_DAYS` | 2 | T+2: cổ phiếu mua chưa về thì chưa bán được |

---

## 14. Lệnh Telegram

| Lệnh | Ví dụ | Chức năng |
|---|---|---|
| `/start`, `/help` | | Giới thiệu và ví dụ câu hỏi |
| `/signal <mã>` | `/signal FPT` | Toàn bộ chi tiết chấm điểm + giải thích AI |
| `/quote <mã>` | `/quote HPG` | Giá hiện tại |
| `/chart <mã> [khung]` | `/chart FPT 3M`, `/chart 1Y HPG` | Biểu đồ nến (khung: 1M, 3M, 6M, 1Y, ALL; cũng hiểu 1T, 3T, 6T, 1N). Không có mã thì dùng mã vừa hỏi |
| `/market` | | Chi tiết cổng thị trường, ngày phân phối |
| `/top` | | Quét và xếp hạng các mã trong `TOP_SYMBOLS` |
| `/watch <mã>`, `/unwatch <mã>`, `/watchlist` | `/watch VNM` | Theo dõi và báo khi tín hiệu đổi |
| `/position add <mã> <giá>` | `/position add FPT 100000` | Lưu vị thế mô phỏng |
| `/position del <mã>`, `/positions` | | Xóa vị thế / xem vị thế kèm lãi/lỗ tạm tính, giá cắt lỗ và trailing stop |
| `/kiemdinh [mã…] [3M\|6M\|1Y\|2Y] [5\|10\|20] [từ [đến]]` (hoặc `/validate`) | `/kiemdinh FPT`, `/kiemdinh FPT 1Y 10` | Bot đúng bao nhiêu case trong quá khứ (mục 18) |
| `/backtest [mã…] [khung] [từ [đến]]` | `/backtest`, `/backtest 3M`, `/backtest FPT HPG 1Y`, `/backtest 2026-01-01 2026-06-30` | Backtest chiến lược. Không tham số: hiện menu nút 🧪 3 tháng / 6 tháng / 1 năm |

**Nút 🧪 Backtest** xuất hiện ở: màn hình `/start`, dưới mọi thẻ phân tích một mã ("🧪 Backtest FPT · 6 tháng"), dưới thẻ so sánh / `/top` ("🧪 Backtest 3 mã này"), và dưới kết quả backtest (📄 Trade Log + Nhật ký tín hiệu CSV, 🔁 chạy lại 3 tháng / 6 tháng / 1 năm). Chat tự nhiên "backtest 3 tháng", "kiểm định chiến lược FPT" cũng chạy được. Lệnh `/backtest` có trong nút Menu của Telegram.

Ngoài lệnh, người dùng có thể **chat tự nhiên** bằng tiếng Việt (có dấu hoặc không dấu).

---

## 15. Kiểm thử

`python selftests.py` chạy **22 nhóm kiểm thử hoàn toàn offline**. Các hàm gọi mạng được thay bằng dữ liệu giả lập ngay trong file test.

| Kiểm thử | Nội dung kiểm tra |
|---|---|
| `test_market_gate` | Đủ 4 trạng thái thị trường; đếm phiên phân phối đúng biên −0.2%; phiên KL giảm hoặc nằm ngoài cửa sổ 25 phiên không được tính |
| `test_tier1` | Các mốc điểm EPS/ROE: 9.99% → 0; 10% → 8 + 5; đủ 30; trường hợp cùng kỳ ≤ 0 |
| `test_t21_delta_roc63` | Biên Delta 15 / 14.99 / 5 / 4.99; căn chỉnh ngày giữa mã và VN-Index; thiếu VN-Index thì T2.1 là "chưa biết" |
| `test_tier2_tier3` | Đủ điểm T2, T3; breakout 3 phiên → 15 điểm |
| `test_breakout_and_volume` | Phát hiện breakout, pivot được đóng băng, hai cửa sổ khối lượng không chồng nhau |
| `test_evaluate_by_market` | Ví dụ ở mục 7.8: BUY/HOLD theo từng trạng thái thị trường và tỷ trọng |
| `test_roe_ttm` | ROE TTM = 400 / 1270 |
| `test_vci_price_board_columns` | Đọc bảng giá VCI dạng cột nhiều tầng, đổi nghìn đồng sang VND |
| `test_source_order` | Thử nguồn đúng thứ tự VCI → MAS → KBS khi nguồn trước lỗi |
| `test_cards_render` | `analyze_symbol` + thẻ `/signal` + thẻ `/market` chạy trọn khi các nguồn phụ lỗi |
| `test_llm_context_grounding` | Dữ liệu gửi cho LLM lấy đúng số của engine |
| `test_multi_symbol_resolution` | Nhận nhiều mã trong câu; không nhầm "ROE" thành mã cổ phiếu |
| `test_conversational_cards` | Thẻ chat và thẻ so sánh hiển thị đủ nội dung |
| `test_conversation_router_and_fallback` | Phân loại ý định; kế thừa mã từ ngữ cảnh; câu trả lời theo quy tắc; loại câu AI chung chung |
| `test_agent_router_offline` | Phân tích, giải thích, giá, so sánh vẫn chạy khi **không có Groq** |
| `test_chart_service` | MA và RSI của biểu đồ khớp công thức engine; PNG/HTML tạo được; caption không chứa BUY/SELL; nhận diện câu yêu cầu biểu đồ |
| `test_ui_cards` | Thanh tiến độ, vị trí giá trong biên độ, thứ tự bản đồ mốc giá, lãi/lỗ vị thế, escape ký tự đặc biệt; mọi thẻ đúng quy tắc HTML của Telegram |
| `test_backtest_helpers` | Quý BCTC được dùng theo ngày công bố (14/05 chưa thấy Q1, 15/05 thấy); cắt cột quý tương lai; MDD; giá trần/sàn HOSE/HNX; khoảng thời gian |
| `test_backtest_engine_rules` | Mua ở Open t+1; khối lượng theo vốn phân bổ, lô 100; T+2; phí + thuế bán 0,1%; bỏ qua lệnh ở giá trần; giới hạn số vị thế; vốn cuối = vốn đầu + lãi/lỗ |
| `test_backtest_matches_live_signal_and_explains` | Điểm/tín hiệu phiên cuối của backtest trùng với engine live (/signal) cùng ngày; mã nợ vay/VCSH 1.86 không bao giờ BUY và thẻ giải thích đúng nút thắt |
| `test_signal_validation` | Quy tắc chấm đúng/sai; lợi nhuận 20 phiên đúng mốc; tín hiệu quá khứ không đổi khi làm sập giá tương lai; trên dữ liệu **có xu hướng** bộ kiểm định phát hiện điểm số có dự báo (✅), trên dữ liệu **ngẫu nhiên** không đưa ra kết luận ✅/❌ nào (không "tìm ra" tác dụng giả) |
| `test_backtest_no_lookahead` | Làm "sập" giá sau một ngày cắt mà mọi lệnh và equity trước ngày đó không đổi; thẻ Telegram, ảnh PNG, Trade Log CSV |

---

## 16. Giới hạn và hướng phát triển

**Giới hạn hiện tại**

- Trạng thái (watchlist, vị thế, bộ nhớ hội thoại) lưu trong RAM, mất khi khởi động lại bot.
- Phụ thuộc vào `vnstock` và API công khai của các công ty chứng khoán. Đổi cấu trúc dữ liệu phía nguồn có thể làm hỏng phần đọc BCTC; `source_adapter.py` và các danh sách tên chỉ tiêu trong `stock_service.py` đã cố gắng hạn chế rủi ro này.
- Phần đọc BCTC từ VCI dùng một hàm nội bộ của vnstock (`_get_financial_report`), vì hàm công khai chỉ trả 4 quý, không đủ để so cùng kỳ.
- Phân loại ý định bằng từ khóa: nhanh và không tốn tiền, nhưng có thể hiểu sai câu diễn đạt lạ. Khi đó câu hỏi rơi vào nhánh `general` để Groq xử lý.
- Backtest chỉ là kiểm định sơ bộ (mặc định 6 tháng, 15 mã): số giao dịch ít, chưa mô phỏng thanh khoản/trượt giá; dữ liệu BCTC là số liệu hiện tại của nguồn (có thể đã điều chỉnh hồi tố) chỉ được "mở khóa" theo ngày công bố giả định. Xem mục 17.
- Danh sách nhóm tài chính (miễn điều kiện nợ) được khai báo tay trong cấu hình.

**Hướng phát triển**

- Lưu trạng thái vào SQLite để giữ watchlist và vị thế qua các lần khởi động.
- Walk-forward / nhiều giai đoạn thị trường cho backtest; dùng ngày công bố BCTC thực tế thay cho độ trễ cố định.
- Đánh dấu các phiên breakout và phiên phân phối lên biểu đồ.
- Phân loại ý định bằng mô hình nhỏ (embedding) thay cho từ khóa.

---

## 17. Backtest chiến lược

Module `backtest.py` hiện thực kế hoạch Backtest của nhóm: kiểm định sơ bộ **đúng bộ quy tắc đang chạy trong bot** (`strategy.evaluate`), không viết một chiến lược riêng cho backtest.

### 17.1. Chuỗi xử lý

```text
Load Configuration → Load Historical Data (+ warm-up 620 ngày) → Validate → [mỗi phiên t]
   Indicator Layer (strategy.compute_technical trên dữ liệu ≤ t)
   → Strategy Layer (market_gate + evaluate: BUY / HOLD / SELL)
   → Backtest Engine (khớp lệnh ở Open t+1) → Portfolio (tiền mặt, vị thế, giới hạn 5 vị thế)
→ Trade Log → Metrics → Report / Chart (PNG, CSV, thẻ Telegram)
```

| Thành phần | Hàm | Ghi chú |
|---|---|---|
| Data Layer | `load_data` | OHLCV mã + VN-Index từ `BACKTEST_START − 630 ngày` tới `BACKTEST_END` qua `stock_service` (cùng nguồn, rate limiter với bot); BCTC tất cả các quý; cache đĩa `data/backtest_cache/` |
| Kiểm tra dữ liệu | `validate_prices` | Cảnh báo thiếu phiên khởi tạo chỉ báo; phiên biến động vượt biên độ (nghi chưa điều chỉnh chia tách/cổ tức) |
| BCTC theo thời điểm | `available_quarter`, `FundamentalAsOf` | Quý Q chỉ được dùng từ ngày cuối quý + 45 ngày; `stock_service.snapshot_from_tables(..., upto=quý)` cắt bỏ các cột quý sau |
| Engine + Portfolio | `run_backtest` | Mô phỏng ngày-qua-ngày, xem 17.2 |
| Metrics | `compute_metrics` | Total Return, Number of Trades, Win Rate, Average Win/Loss, Profit Factor, Maximum Drawdown, Average Holding Period, so với VN-Index |
| Report | `render_report_png`, `trade_log_csv`, `equity_csv`, `presentation.backtest_card` | Equity Curve vs VN-Index, Drawdown Chart, tỷ lệ vốn đầu tư; Trade Log; Performance Summary |

### 17.2. Quy tắc mô phỏng

1. **Không look-ahead**: tại phiên t, chiến lược chỉ nhận cửa sổ nến trong (t − 620 ngày, t] — đúng cửa sổ bot live nhìn thấy — và VN-Index cắt tới t. Tín hiệu phiên cuối không được thực hiện.
2. **Khớp lệnh t+1**: lệnh bán xử lý trước (giải phóng tiền), rồi tới lệnh mua theo thứ tự điểm cao → Delta ROC63 cao (giống `/top`).
3. **Điều kiện mua** (theo đúng dòng "Nếu mua phiên kế tiếp…" trong tín hiệu BUY): Open t+1 > Pivot và ≤ vùng mua. Open t+1 ở **giá trần** (HOSE ±7%, HNX ±10%) → **BỎ QUA**, ghi lý do, không giả định khớp.
4. **Phân bổ vốn**: mỗi vị thế = NAV tại ngày tín hiệu / `MAX_POSITIONS` (≈ 20 triệu với 100 triệu và 5 vị thế), nhân hệ số cổng thị trường (PRESSURE = ½, tắt bằng `BACKTEST_APPLY_WEIGHT_FACTOR=false`); làm tròn xuống lô 100 cổ phiếu; không margin; thiếu tiền hoặc đủ 5 vị thế thì bỏ qua.
5. **Bán**: khi `evaluate` trả SELL (cắt lỗ 7%, trailing, breakdown, gãy MA50, thị trường DOWNTREND) → bán ở Open phiên kế tiếp, nhưng phải đủ **T+2** kể từ ngày mua; phiên trắng bên mua ở giá sàn → dời sang phiên sau.
6. **Chi phí**: phí `TRANSACTION_FEE` cả hai chiều + thuế bán `SELL_TAX` 0,1%, trừ thẳng vào lãi/lỗ.
7. **Cuối kỳ**: vị thế còn mở được đóng theo giá đóng cửa phiên cuối (có phí + thuế) để Trade Log đầy đủ; dòng này ghi rõ "Đóng cuối kỳ".
8. **Phiên cuối chỉ để đối chiếu**: vẫn được chấm điểm (không đặt lệnh) để so với thẻ `/signal` cùng ngày — hai kết quả phải trùng nhau.

### 17.2b. Chẩn đoán "vì sao có / không có lệnh"

Mỗi phiên × mã được ghi vào **nhật ký tín hiệu** (`signal_log.csv`, nút 📄 trên Telegram): thị trường, tín hiệu, điểm T1/T2/T3, ngưỡng BUY, 6 điều kiện BUY (✓/✗), bộ lọc cứng bị trượt, quý BCTC đang dùng, lý do. Thẻ kết quả tổng hợp từ nhật ký này:

- số phiên cổng thị trường chặn mua mới (WEAK/DOWNTREND);
- với từng mã: số phiên trượt bộ lọc cứng (kèm giá trị, VD "Nợ vay/VCSH > 1.5 (gần nhất 1.86)"), không có breakout, điểm dưới ngưỡng, điểm cao nhất trong kỳ, phiên "gần BUY nhất" và điều kiện còn thiếu;
- danh mục nhiều mã: bảng `Mã | BUY | Max | Nút thắt chính`.

Nhờ vậy kết quả "0 giao dịch" được giải thích rõ: ví dụ VIC có nợ vay/VCSH 1.86 > 1.5 nên bị bộ lọc cứng chặn mọi phiên; VNG trượt cả thanh khoản lẫn nợ vay; FPT qua bộ lọc nhưng không có breakout hợp lệ và điểm cao nhất vẫn dưới ngưỡng.

### 17.3. Cách chạy

```bash
python backtest.py                                   # 15 mã, 6 tháng gần nhất
python backtest.py --period 3M
python backtest.py --symbols FPT,HPG --start 2026-01-01 --end 2026-06-30
```

Kết quả lưu ở `data/backtest_reports/<thời điểm>/`: `summary.txt`, `trade_log.csv`, `signal_log.csv`, `equity.csv`, `backtest.png`.

Trên Telegram: bấm **🧪 Backtest chiến lược** ở `/start` (hoặc nút 🧪 dưới thẻ phân tích), chọn khung. Bot chạy nền, cập nhật tiến độ trên một tin nhắn, rồi gửi ảnh + thẻ kết quả kèm nút **📄 Trade Log + Equity (CSV)** và **🔁** chạy lại khung khác. Mỗi lúc chỉ một backtest chạy (dùng chung quota VNStock).

**Thời gian**: lần đầu 15 mã cần khoảng 62 lượt gọi VNStock (giá + BCTC) → ~4 phút với quota khách 16 lượt/phút, ~1–2 phút khi có API key. Các lần sau dùng cache đĩa (12 giờ; khoảng đã khép thì 30 ngày), phần mô phỏng mất khoảng 10–15 giây.

---

## 18. Kiểm định độ chính xác tín hiệu (test ở quá khứ)

**Câu hỏi:** hôm nay bot nói BUY / HOLD / SELL nhưng ta không xem được tương lai. Vậy **cách tính của bot có đúng không, có đáng tin không?** Cách trả lời: quay về quá khứ (VD 24/03 → 24/09), ở mỗi phiên cho bot chạy lại y như hôm đó, rồi dùng giá thực tế sau đó làm **nhãn thật** để đếm bot đúng bao nhiêu case.

### 18.1. Quy trình (`signal_validation.py`)

```text
[mỗi mã, mỗi phiên t trong giai đoạn]
  backtest.Replay: dữ liệu ≤ t, VN-Index ≤ t, BCTC đã công bố tại t
  → strategy.compute_technical + strategy.evaluate (không sửa dòng nào)
  → kết luận của bot: BUY / SELL / NẮM GIỮ / TRÁNH MUA / CHỜ
[sau khi replay xong — dữ liệu tương lai KHÔNG đi vào bước trên]
  nhãn thật = giá sau H phiên (mặc định 20) so với giá ngày t → TĂNG / GIẢM / ĐI NGANG
  chấm: ĐÚNG / SAI / BỎ LỠ / KHÔNG BỎ LỠ / CHƯA CHẤM
```

| Bot nói | ĐÚNG khi (sau H phiên) | SAI khi |
|---|---|---|
| BUY | giá tăng | không tăng |
| SELL | giá giảm tiếp | không giảm |
| NẮM GIỮ | giá không giảm | giảm |
| TRÁNH MUA | giá không tăng | tăng |
| CHỜ | không chấm; **BỎ LỠ** nếu sau đó tăng > 10% | |

Mỗi mã có một vị thế ảo đi theo đúng khuyến nghị (BUY → mua; SELL → bán) nên cả NẮM GIỮ và SELL cũng được chấm. Riêng "kết luận hôm nay" được tính cho người **chưa có vị thế**, giống thẻ `/signal`.

Các phiên cuối chưa đủ H phiên tương lai được đánh dấu **CHƯA CHẤM** (VD chạy 24/09 với H = 20 thì các phiên từ ~27/08 chưa chấm được).

### 18.2. Có đáng tin không? — so với "đoán bừa"

Tỷ lệ đúng tự nó không đủ: nếu FPT tăng 78% số phiên thì nói NẮM GIỮ lúc nào cũng "đúng" 78%. Vì vậy mỗi kết luận được so với **đoán bừa** = xác suất đúng nếu nói cùng kết luận đó mà không nhìn gì (BUY đúng với xác suất = tỷ lệ phiên mã tăng; TRÁNH MUA = tỷ lệ phiên không tăng…), tính riêng cho từng mã.

- ✅ bot tốt hơn đoán bừa rõ ràng · ❌ kém hơn rõ ràng · 🟡 có dấu hiệu, chưa chắc · ⚪ chưa khác đoán bừa (có thể do may rủi) hoặc quá ít case.
- Mức tin cậy dùng kiểm định z có hiệu chỉnh cửa sổ chồng nhau (các ngày liền nhau của cùng một mã không được tính là các lần thử độc lập).
- Kiểm thử `test_signal_validation` xác nhận: trên dữ liệu có xu hướng bộ kiểm định báo ✅; trên dữ liệu ngẫu nhiên nó **không** báo ✅/❌.

### 18.3. Báo cáo gồm

- **Bot đúng K/N case = x%** so với đoán bừa, kèm mức tin cậy.
- Bảng theo từng kết luận (đúng/chấm, %, đoán bừa); CHỜ: tỷ lệ bỏ lỡ.
- Ma trận "bot nói gì → thực tế TĂNG / ĐI NGANG / GIẢM".
- Độ chính xác ở 3 tầm nhìn 5 / 10 / 20 phiên; theo tháng (1 mã) hoặc theo mã (nhiều mã).
- **Kết luận hôm nay đáng tin tới đâu**: "hôm nay bot nói TRÁNH MUA → trong quá khứ khi nói TRÁNH MUA với mã này, đúng 55/84 = 65% (đoán bừa 58%)".
- Nhận xét bằng lời: case đúng rõ nhất, sai nặng nhất, bỏ lỡ lớn nhất, tháng tốt/kém nhất và **KẾT LUẬN** có đáng tin không.
- Ảnh: đường giá với mỗi phiên là một chấm (màu = đúng/sai, hình = kết luận) + tỷ lệ đúng 20 case gần nhất so với đoán bừa; ảnh tổng hợp theo kết luận / tháng / tầm nhìn.
- CSV từng case: ngày, giá, kết luận, điểm, thay đổi giá sau 5/10/20 phiên, thực tế, **ĐÚNG/SAI**, nhận xét, lý do của bot.
- Phần nâng cao (thu gọn): IC điểm số, cổng thị trường, lệnh ảo BUY→SELL, đối chiếu pipeline với engine live.

### 18.4. Cách chạy

```bash
python signal_validation.py --symbols FPT                  # 6 tháng gần nhất, chấm sau 20 phiên
python signal_validation.py --symbols FPT --horizon 10
python signal_validation.py --period 1Y                    # 15 mã mặc định
python signal_validation.py --symbols FPT,HPG --start 2025-10-01 --end 2026-06-30
```

Kết quả ở `data/backtest_reports/validation_<thời điểm>/`: `summary.txt`, `cases.csv`, `accuracy.png`, `accuracy_<mã>.png`, `advanced.png`, `virtual_trades.csv`.

Trên Telegram: nút **🔬 Độ chính xác FPT (6 tháng)** dưới thẻ phân tích mã; menu `/start`; `/kiemdinh FPT`, `/kiemdinh FPT 1Y 10` (10 = chấm sau 10 phiên); hoặc chat "kiểm định tín hiệu FPT 6 tháng". Dưới kết quả có nút **Chấm sau 5 / 10 / 20 phiên** (chấm lại tức thì, không tải lại dữ liệu) và **📄** tải CSV từng case.

Cấu hình trong `.env`: `VALIDATION_HORIZON` (20), `VALIDATION_FLAT_BAND` (0 — biên % coi là đi ngang), `VALIDATION_MISSED_GAIN` (10), `VALIDATION_MONTHS` (6).

---

## Giấy phép thư viện bên thứ ba

- **TradingView Lightweight Charts™** v5.0.9: Apache License 2.0 (`static/LIGHTWEIGHT_CHARTS_LICENSE.txt`). Logo TradingView trên biểu đồ tương tác được giữ theo yêu cầu ghi công của thư viện.
- **vnstock**: tuân theo điều khoản sử dụng của tác giả thư viện và các nguồn dữ liệu.
