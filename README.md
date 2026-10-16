# Stock Opportunity Analyzer

Ứng dụng web phân tích cổ phiếu Việt Nam, kết hợp dữ liệu giá và báo cáo tài chính để hiển thị phân tích cơ bản, phân tích kỹ thuật, tín hiệu tham khảo, backtest và báo cáo PDF. Máy chủ web sử dụng HTTP server có sẵn trong Python, mặc định chạy riêng trên máy của bạn tại `127.0.0.1:8050`.

> Công cụ phục vụ học tập và tham khảo, không phải khuyến nghị đầu tư cá nhân hóa.

## Tính năng

- Tìm kiếm mã và xem hồ sơ doanh nghiệp, tài chính cơ bản, xu hướng giá, chỉ báo kỹ thuật và tin tức.
- Chấm điểm cơ bản/kỹ thuật theo trọng số 60/40 và hiển thị độ phủ dữ liệu.
- Định giá chuyên sâu 3 kịch bản CHỈ chạy khi tín hiệu L2 là MUA: Groq đề xuất giả định, ứng dụng tự tính giá mục tiêu P/E hoặc P/B; tín hiệu khác trả về SKIPPED.
- Chạy backtest 1 năm; so sánh với Buy & Hold và VN-Index; xem nhật ký lệnh, trạng thái từng tín hiệu và chất lượng dữ liệu.
- Backtest giả lập vốn 100 triệu VNĐ, lô 100 cổ phiếu, phí mua/bán 0,10%, thuế bán 0,10% và trượt giá 0,10%; đây là mô phỏng tham khảo, chưa bao gồm cổ tức và thanh khoản thực tế.
- So sánh cổ phiếu cùng ngành và xuất báo cáo PDF.

## Yêu cầu

- Windows 10/11 và Python 3.10 trở lên.
- Kết nối Internet để cài thư viện và lấy dữ liệu thị trường.
- Không cần cài Node.js, Flask hoặc cơ sở dữ liệu riêng để chạy web.

## Cài đặt trên Windows

Mở PowerShell tại thư mục dự án có chứa `web_app.py` và `requirements.txt`. Nếu vừa clone toàn bộ repository GitHub, chạy:

```powershell
cd Stock_Opportunity_Analyzer_Final\Stock_Opportunity_Analyzer_Final
```

Nếu bạn đã ở thư mục có `web_app.py`, bỏ qua lệnh `cd`. Tạo môi trường ảo và cài thư viện:

```powershell
py -3 --version
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Các lệnh dùng trực tiếp Python trong `.venv`, nên không cần bật môi trường ảo hay thay đổi Execution Policy của PowerShell. Nếu `py -3 --version` không chạy được, cài Python 3.10 trở lên từ <https://www.python.org/downloads/> rồi mở lại PowerShell.

## Cấu hình (không bắt buộc)

Web có thể khởi động mà không cần API key. Dữ liệu thị trường vẫn cần kết nối Internet và có thể chịu giới hạn truy cập/quota của nguồn dữ liệu.

Để bật hoặc cải thiện các tính năng AI, tạo file `.env` trong thư mục chứa `web_app.py` bằng Notepad, rồi thêm những biến bạn có key:

```dotenv
GROQ_API_KEY=điền_key_Groq_của_bạn
```

- `GROQ_API_KEY`: dùng cho giả định định giá 3 kịch bản, hồ sơ doanh nghiệp và tin tức. Thiếu key thì phần định giá báo AI_UNAVAILABLE, các phần khác vẫn chạy. Xem thêm `.env.example`.
- `VNSTOCK_API_KEY`: tùy chọn; có thể cấu hình để dùng quota VNStock phù hợp với tài khoản của bạn.
- `TELEGRAM_BOT_TOKEN`: chỉ cần khi chạy Telegram bot, không cần để chạy web.

Không đưa `.env` hoặc API key lên GitHub, không chia sẻ key trong ảnh chụp màn hình hay chat. File `.env` đã được loại khỏi Git bằng `.gitignore`.

Một số tùy chọn máy chủ có thể đặt trong `.env`:

```dotenv
DASHBOARD_HOST=127.0.0.1
DASHBOARD_PORT=8050
```

`127.0.0.1` chỉ cho phép truy cập từ máy đang chạy ứng dụng. Không đổi host sang địa chỉ công khai nếu chưa cấu hình bảo vệ và hiểu rõ rủi ro khi mở máy chủ ra Internet.

## Chạy website

Từ thư mục có `web_app.py`, chạy:

```powershell
.\.venv\Scripts\python.exe web_app.py
```

Giữ cửa sổ PowerShell đang chạy máy chủ, rồi mở:

- Trang chủ: <http://127.0.0.1:8050/>
- Trang phân tích mẫu: <http://127.0.0.1:8050/analysis/FPT>
- Kiểm tra máy chủ: <http://127.0.0.1:8050/healthz>

Nhập mã cổ phiếu trên trang chủ để phân tích mã khác. Dừng máy chủ bằng `Ctrl+C`.

## Xử lý lỗi thường gặp

- **`No module named 'google'` hoặc `No module named 'vnstock'`:** kiểm tra đang ở đúng thư mục dự án và chạy lại `.\.venv\Scripts\python.exe -m pip install -r requirements.txt`.
- **Cổng 8050 đang được sử dụng:** đóng tiến trình đang dùng cổng đó hoặc đặt `DASHBOARD_PORT=8051` trong `.env`, sau đó mở `http://127.0.0.1:8051/`.
- **Thiếu dữ liệu/mã không phân tích được:** kiểm tra Internet, mã cổ phiếu và thử lại sau; nguồn dữ liệu bên ngoài có thể giới hạn quota hoặc tạm thời gián đoạn.
- **Chatbot/AI không có phản hồi AI:** kiểm tra API key tương ứng trong `.env`. Web và các phần phân tích không yêu cầu Telegram token.
- **PowerShell không nhận `py`:** cài Python từ trang chính thức, bật Python Launcher trong bộ cài nếu có, rồi mở cửa sổ PowerShell mới.

## Chạy kiểm thử

Một số kiểm thử tự chạy của dự án có thể chạy bằng:

```powershell
.\.venv\Scripts\python.exe selftests.py
.\.venv\Scripts\python.exe investment_selftests.py
.\.venv\Scripts\python.exe final_selftests.py
```

## Cấu trúc chính

```text
web_app.py                    Điểm khởi chạy website
dashboard_server.py           HTTP routes và API
investment_analysis.py        Phân tích và chấm điểm
stock_service.py               Lấy dữ liệu giá/thị trường
chart_service.py               Chuẩn bị dữ liệu biểu đồ
valuation_forecast_service.py  Dự phóng và định giá
news_service.py                Tin tức doanh nghiệp
report_pdf_new.py              Báo cáo PDF
requirements.txt               Thư viện Python
data/valuation_reference.json  Tham chiếu định giá
templates/                     Giao diện web
static/                        Tài nguyên giao diện/biểu đồ
```
