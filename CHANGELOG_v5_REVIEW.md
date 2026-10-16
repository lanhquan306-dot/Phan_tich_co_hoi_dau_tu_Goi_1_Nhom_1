# v5 – Sửa định giá chuyên sâu và backtest theo đặc tả Gói 1

## Định giá 3 kịch bản (`valuation_forecast_service.py`)
- Khôi phục điều kiện chạy theo đặc tả "Gói 1 - GK", Bước 1: chỉ chạy khi tín hiệu L2 là `BUY`/`MUA`;
  tín hiệu khác trả về `status = "SKIPPED"` và KHÔNG gọi Groq. Bản v4 đã bỏ điều kiện này
  nên mã đang GIỮ (ví dụ FPT) vẫn hiện STRONG BUY ở mục 3.
- Truyền `current_pe`/`current_pb` cho Groq làm mốc bội số kịch bản cơ sở; cảnh báo khi bội số
  cơ sở lệch quá 30% so với hiện tại.
- Thêm cảnh báo: upside là lũy kế tới cuối năm thứ 3, chưa chiết khấu, chưa tính cổ tức.
- Sửa thông báo thiếu vốn chủ sở hữu cho khớp kiểm thử ("vốn chủ sở hữu/BVPS").

## Dữ liệu lịch sử (`investment_analysis.py`)
- `_financial_history` ưu tiên LNST thuộc cổ đông công ty mẹ (trước đây ưu tiên LNST tổng gồm cả
  cổ đông không kiểm soát, làm EPS dự phóng cao hơn thực tế ở doanh nghiệp có công ty con).

## Backtest (`opportunity_backtest.py`, `dashboard_server.py`)
- Theo thống nhất của nhóm, chỉ backtest 1 năm (`?months=12` hoặc `?years=1`); kỳ khác trả lỗi 400.
- Tự tải thêm lịch sử khi nguồn trả thiếu phiên cho giai đoạn khởi tạo chỉ báo.
- Tín hiệu tính sau MỖI phiên (trước là 20 phiên/lần), khớp giá mở cửa phiên kế tiếp.
  Chỉ báo tính một lần rồi cắt tiền tố – đã kiểm chứng cho kết quả giống hệt tính lại từng phiên.
- Không tự bán cuối kỳ: vị thế mở được định giá theo giá đóng cửa và báo trong `open_position`;
  số giao dịch hoàn tất và win rate chỉ tính chu kỳ đã đóng.
- Trạng thái từng tín hiệu: EXECUTED, IGNORED_ALREADY_LONG, IGNORED_NO_POSITION,
  REJECTED_INSUFFICIENT_CASH, NOT_EXECUTED_NO_PRICE, NOT_EXECUTED_END_OF_PERIOD.
- So sánh VN-Index (quy đổi từ 100 triệu), Max Drawdown cho cả Buy & Hold và VN-Index.
- Trả về đủ `summary`, `orders`, `equity_curve`, `data_quality`, `config`, `status`;
  giữ các khóa cũ cho giao diện. Thiếu dữ liệu trả HTTP 422 `INSUFFICIENT_DATA`.

## Lý do tín hiệu GIỮ (`investment_analysis.py`)
- Xu hướng "Tích cực" khi MA20 > MA50 và RSI14 > 50 (RSI trước là ≥ 45). Theo quyết định của nhóm,
  KHÔNG bắt buộc giá > MA20; đây là điểm nới lỏng so với mục 4 tài liệu ("giá duy trì trên các đường trung bình").
- MUA bị chặn khi phiên gần nhất giảm giá kèm khối lượng ≥ 1,5 lần TB20 ("giá giảm kèm áp lực bán";
  nhóm nới lỏng từ 1,2 lên 1,5, hằng số `SELLING_PRESSURE_VOL_RATIO`). Điểm khối lượng vẫn dùng mốc 1,2.
- Quy ước cho trường hợp mục 5.1 chưa nêu: kỹ thuật trung tính = chưa xác nhận xu hướng, S ≥ 70 vẫn là HOLD.
  Ghi trong `methodology.hold_rule` và trang Phương pháp luận.
- Khi S ≥ 70 mà vẫn GIỮ, lý do nêu cụ thể điều kiện chưa đạt (ví dụ "giá chưa nằm trên MA20");
  trả thêm `buy_checks` và giao diện hiển thị danh sách Đạt / Chưa đạt.

## Giao diện và tài liệu
- Mục 3 hiển thị thông báo SKIPPED khi tín hiệu không phải MUA; đổi nhãn "Giả định Gemini" → "Groq".
- Tab backtest: một nút 1 năm, bảng so sánh 3 phương pháp, nhật ký lệnh, trạng thái tín hiệu.
- README: bỏ hướng dẫn Gemini, cập nhật Groq và kỳ backtest.

## Kiểm thử
- `selftests.py` 22/22, `investment_selftests.py` 13/13 (thêm TC06, TC08, TC09, TC10, TC12, TC13, TC14 và kiểm thử lý do GIỮ),
  `final_selftests.py` 4/4.
