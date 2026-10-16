# Stock Opportunity Analyzer – FinTech Copilot

Ứng dụng hỗ trợ phân tích cổ phiếu Việt Nam, kết hợp dữ liệu thị trường, chỉ tiêu tài chính, phân tích kỹ thuật, bộ quy tắc tạo tín hiệu tham khảo, công cụ backtest và giao diện tương tác qua website/Telegram.

> **Lưu ý:** Đây là dự án phục vụ học tập, nghiên cứu và tham khảo. Kết quả phân tích không phải khuyến nghị đầu tư cá nhân hóa và không bảo đảm lợi nhuận.

## 1. Tính năng chính

- Tra cứu mã cổ phiếu và dữ liệu giá lịch sử.
- Tổng hợp thông tin doanh nghiệp và các chỉ tiêu tài chính khi nguồn dữ liệu có cung cấp.
- Tính toán chỉ báo kỹ thuật như đường trung bình động, RSI, ROC và các điều kiện breakout.
- Chấm điểm theo quy tắc và tạo tín hiệu tham khảo BUY/HOLD/SELL hoặc trạng thái tương ứng của từng phân hệ.
- Theo dõi trạng thái thị trường chung và áp dụng điều kiện lọc tín hiệu.
- Phân tích, so sánh cổ phiếu và hỗ trợ giải thích kết quả bằng chatbot.
- Hiển thị biểu đồ và cung cấp báo cáo PDF.
- Kiểm định tín hiệu lịch sử và mô phỏng chiến lược giao dịch (backtest).
- Website chạy cục bộ; Telegram bot là thành phần riêng và cần Telegram Bot Token.

Một số chức năng AI phụ thuộc vào API key và trạng thái dịch vụ bên ngoài. Nếu chưa cấu hình Groq, các luồng trả lời theo quy tắc vẫn có thể hoạt động ở những chức năng đã được thiết kế hỗ trợ dự phòng.

## 2. Công nghệ

- **Python** – ngôn ngữ chính.
- **Pandas, NumPy** – xử lý dữ liệu và tính toán.
- **VNStock** – truy xuất dữ liệu thị trường theo nguồn được hỗ trợ.
- **Matplotlib** – tạo biểu đồ.
- **python-telegram-bot** – tích hợp Telegram.
- **OpenAI Python SDK** – gọi API tương thích OpenAI; trong cấu hình chatbot, endpoint được trỏ đến Groq.
- **Google GenAI SDK** – hỗ trợ những chức năng AI được cấu hình trong các mô-đun liên quan.
- **python-dotenv** – nạp biến môi trường từ tệp `.env`.
- **HTTP server tích hợp của Python** – phục vụ website, không yêu cầu Flask để khởi chạy máy chủ web chính.

## 3. Yêu cầu hệ thống

- Windows 10/11 hoặc hệ điều hành có Python tương thích.
- Python 3.10 trở lên; nên dùng một phiên bản Python 3.x ổn định tương thích với các thư viện trong `requirements.txt`.
- Kết nối Internet để cài thư viện, truy xuất dữ liệu thị trường và gọi dịch vụ API.
- API key tương ứng nếu muốn sử dụng các tính năng AI hoặc nguồn dữ liệu yêu cầu xác thực.

## 4. Cài đặt trên Windows

### Bước 1: Giải nén và mở đúng thư mục

Giải nén dự án, sau đó mở PowerShell tại thư mục chứa các tệp `web_app.py`, `bot.py` và `requirements.txt`.

Nếu PowerShell đang ở thư mục ngoài, dùng `cd` để chuyển đến thư mục dự án. Đường dẫn chính xác tùy vị trí bạn giải nén.

### Bước 2: Tạo môi trường ảo

```powershell
py -3 --version
py -3 -m venv .venv
```

### Bước 3: Cài đặt thư viện

```powershell
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Nếu lệnh `py` không hoạt động, hãy cài Python từ trang chính thức: <https://www.python.org/downloads/> rồi mở lại PowerShell.

## 5. Cấu hình biến môi trường/API

1. Tìm tệp `.env.example` trong thư mục dự án.
2. Sao chép tệp đó thành `.env` trong cùng thư mục với `config.py`.
3. Mở `.env` bằng trình soạn thảo văn bản và điền các khóa API của chính bạn vào đúng biến được khai báo trong tệp mẫu.
4. Không đăng tải `.env` lên GitHub hoặc gửi API key qua ảnh chụp màn hình, nhóm chat hay báo cáo.

Các biến quan trọng thường gặp trong cấu hình dự án:

| Biến | Mục đích |
|---|---|
| `GROQ_API_KEY` | API key để chatbot sử dụng mô hình ngôn ngữ qua Groq. |
| `GROQ_MODEL` | Tên mô hình Groq; mã nguồn đặt mặc định `openai/gpt-oss-20b` nếu không cấu hình giá trị khác. |
| `TELEGRAM_BOT_TOKEN` | Token do BotFather cấp để chạy bot Telegram. |
| `VNSTOCK_API_KEY` | Tùy chọn, sử dụng khi cần xác thực/quota VNStock theo cấu hình. |
| Các biến Google GenAI | Chỉ cần khi sử dụng những chức năng Google AI tương ứng; hãy kiểm tra tên biến chính xác trong `.env.example` và `config.py`. |

Ví dụ minh họa cấu hình chatbot (không phải API key thật):

```dotenv
GROQ_API_KEY=your_groq_api_key_here
GROQ_MODEL=openai/gpt-oss-20b
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
```

Lấy Groq API key tại <https://console.groq.com/keys>.

Tạo Telegram bot và lấy token qua <https://t.me/BotFather>. Không gửi token cho người khác vì token cho phép điều khiển bot.

Nếu không dùng chức năng Telegram, không cần cấu hình `TELEGRAM_BOT_TOKEN` để chạy website. Một số chức năng AI khác có thể yêu cầu khóa Google GenAI riêng; xem `.env.example` để biết biến tương ứng của phiên bản đang sử dụng.

## 6. Chạy website

Từ thư mục chứa `web_app.py`, chạy:

```powershell
.\.venv\Scripts\python.exe web_app.py
```

Sau khi máy chủ khởi động, mở trình duyệt tại:

- Trang chủ: <http://127.0.0.1:8050/>
- Trang phân tích mẫu (nếu route được bật): <http://127.0.0.1:8050/analysis/FPT>
- Endpoint kiểm tra tình trạng máy chủ (nếu có): <http://127.0.0.1:8050/healthz>

Địa chỉ và cổng mặc định có thể thay đổi qua cấu hình. Giữ cửa sổ terminal mở trong khi sử dụng website. Nhấn `Ctrl+C` để dừng máy chủ.

## 7. Chạy Telegram bot

Đảm bảo đã điền `TELEGRAM_BOT_TOKEN` trong `.env`, sau đó mở một cửa sổ PowerShell khác tại thư mục dự án và chạy:

```powershell
.\.venv\Scripts\python.exe bot.py
```

Nếu bot không khởi động:

- Kiểm tra token đã được điền đúng và không có dấu cách thừa.
- Kiểm tra kết nối Internet.
- Kiểm tra thư viện đã được cài bằng `requirements.txt`.
- Không công khai token trong log, ảnh chụp màn hình hoặc kho mã nguồn.

Website và Telegram bot là hai tiến trình riêng. Nếu cần sử dụng đồng thời cả hai, hãy chạy mỗi tiến trình trong một cửa sổ terminal riêng.

## 8. Kiểm thử và backtest

Dự án có các tệp kiểm thử và công cụ kiểm định. Có thể thử chạy:

```powershell
.\.venv\Scripts\python.exe selftests.py
.\.venv\Scripts\python.exe investment_selftests.py
.\.venv\Scripts\python.exe final_selftests.py
```

Chỉ chạy những tệp kiểm thử thực sự có trong phiên bản dự án bạn đang dùng. Ghi lại số lượng kiểm thử thành công/thất bại và thông báo lỗi nếu cần đưa kết quả vào báo cáo.

Các mô-đun backtest/validation có thể được gọi theo giao diện mà mã nguồn định nghĩa. Trước khi công bố kết quả, cần chạy kiểm định thực tế và ghi nhận ít nhất lợi nhuận ròng, số giao dịch, win rate, maximum drawdown và kết quả so sánh với chỉ số tham chiếu. Không suy diễn hiệu quả đầu tư chỉ từ việc mã nguồn có chức năng backtest.

## 9. Cấu trúc thư mục và các mô-đun quan trọng

```text
.
├── bot.py                       # Điểm khởi chạy Telegram bot
├── web_app.py                   # Điểm khởi chạy website
├── dashboard_server.py          # HTTP routes/API và điều phối giao diện web
├── config.py                    # Đọc cấu hình và biến môi trường
├── conversation.py               # Xử lý ý định/ngữ cảnh hội thoại
├── llm.py                        # Điều phối câu trả lời AI/Groq và tool calling
├── groq_service.py               # Thành phần dịch vụ Groq
├── stock_service.py              # Truy xuất và xử lý dữ liệu cổ phiếu
├── source_adapter.py              # Lớp thích ứng nguồn dữ liệu
├── company_info_service.py       # Thông tin doanh nghiệp
├── investment_analysis.py        # Phân tích/chấm điểm đầu tư trên web
├── strategy.py                   # Quy tắc chiến lược và tín hiệu giao dịch
├── chart_service.py               # Chuẩn bị biểu đồ
├── news_service.py                # Thu thập/xử lý tin tức
├── valuation_forecast_service.py # Dự phóng và định giá theo cấu hình
├── backtest.py                    # Mô phỏng chiến lược/danh mục
├── opportunity_backtest.py       # Kiểm định cơ hội đầu tư
├── signal_validation.py          # Đánh giá tín hiệu lịch sử
├── report_pdf_new.py              # Tạo báo cáo PDF
├── requirements.txt               # Danh sách thư viện Python
├── .env.example                   # Mẫu biến môi trường (nếu có trong gói)
├── templates/                     # HTML giao diện
└── static/                        # Tài nguyên tĩnh/biểu đồ
```

Danh sách trên mô tả các mô-đun tiêu biểu; các tệp phụ trợ khác có thể tồn tại trong thư mục dự án.

## 10. Xử lý lỗi thường gặp

- **Không tìm thấy module:** xác nhận terminal đang ở thư mục dự án và cài lại thư viện bằng `pip install -r requirements.txt` trong môi trường `.venv`.
- **Không kết nối được dữ liệu:** kiểm tra Internet, mã cổ phiếu, quota hoặc tình trạng của nhà cung cấp dữ liệu; thử lại sau nếu dịch vụ tạm thời gián đoạn.
- **Groq báo thiếu API key hoặc lỗi xác thực:** kiểm tra `GROQ_API_KEY`, tên biến trong `.env`, quyền truy cập và trạng thái quota trên Groq Console.
- **Telegram bot không phản hồi:** kiểm tra `TELEGRAM_BOT_TOKEN`, bảo đảm tiến trình `bot.py` vẫn đang chạy và kiểm tra log lỗi trong terminal.
- **Cổng web đã được sử dụng:** thay cổng trong biến cấu hình mà `config.py` hỗ trợ, rồi truy cập đúng địa chỉ/cổng mới.
- **AI không khả dụng:** một số luồng có thể chuyển sang câu trả lời theo quy tắc; các chức năng phụ thuộc trực tiếp vào API bên ngoài sẽ cần API key hợp lệ và kết nối hoạt động.
- **Dữ liệu hoặc kết quả thiếu:** kết quả phụ thuộc vào dữ liệu do nguồn cung cấp. Không coi dữ liệu thiếu là giá trị bằng 0 và không sử dụng tín hiệu khi chưa hiểu cảnh báo chất lượng dữ liệu.

## 11. An toàn và giới hạn sử dụng

- Không lưu API key trực tiếp trong mã nguồn.
- Không chia sẻ `.env`, Telegram token hoặc API key.
- Máy chủ mặc định bind vào `127.0.0.1`, phù hợp cho chạy cục bộ. Không mở ra Internet công khai nếu chưa thiết lập xác thực, kiểm soát truy cập và biện pháp bảo mật phù hợp.
- Kết quả phân tích và backtest phụ thuộc vào dữ liệu, giả định mô phỏng và cấu hình. Hiệu quả trong quá khứ không bảo đảm kết quả tương lai.
- Kiểm tra nguồn dữ liệu, thời điểm công bố báo cáo tài chính, chi phí giao dịch, thanh khoản và các sự kiện doanh nghiệp trước khi diễn giải kết quả.

## 12. Giấy phép và ghi nhận

Kiểm tra các tệp giấy phép/ghi nhận của thư viện bên thứ ba trước khi phân phối lại dự án. Nếu sử dụng mã nguồn này trong báo cáo học thuật, hãy ghi rõ phạm vi chức năng đã triển khai, kết quả kiểm thử thực tế và những phần còn phụ thuộc vào API/dữ liệu bên ngoài.
