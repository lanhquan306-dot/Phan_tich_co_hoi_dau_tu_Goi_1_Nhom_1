# Thay đổi cho bài giữa kỳ - Web phân tích cơ hội đầu tư

1. Thêm `investment_analysis.py`: tăng trưởng doanh thu/LNST, ROE, ROA, D/E, P/E, P/B; phân biệt doanh nghiệp tài chính; MA20/MA50/RSI14; trend; scoring; handoff TV1/TV4.
2. Thêm `templates/investment_dashboard.html`: giao diện web chính, search ticker, score, tài chính, kỹ thuật, chart, strengths/weaknesses/risks, chatbot bên phải.
3. Nâng `dashboard_server.py`: `/analysis/<symbol>`, `/api/analysis/<symbol>`, `/report/<symbol>.pdf`, `/api/chat`, giữ `/chart/<symbol>`.
4. Thêm `report_pdf.py`: sinh PDF theo yêu cầu người dùng.
5. Thêm `investment_chat.py`: chatbot sidebar, dùng Groq nếu có key và fallback rule-based nếu không có.
6. Thêm `web_app.py`: chạy website độc lập với Telegram.
7. Thêm `data/valuation_reference.json`: chỗ nhập P/E/P/B tham chiếu đã kiểm chứng.
8. `config.py`: thêm BANK/SECURITIES/INSURANCE symbol sets.
9. `bot.py`: nút dashboard mở trang phân tích mới.
10. `.env` thật không được đưa vào gói bàn giao; dùng `.env.example`.

Kiểm thử: `python selftests.py` -> ALL PASS (22 nhóm). PDF demo đã được render kiểm tra bố cục.
