# config/investment_profiles.py
from typing import Dict, Any

INVESTMENT_PROFILES: Dict[str, Dict[str, Any]] = {
    "conservative": {
        "name": "Thận trọng",
        "description": "Ưu tiên bảo toàn vốn, sức khỏe tài chính và biên an toàn cao.",
        "required_margin_of_safety": 0.25,     # Yêu cầu giá thị trường thấp hơn giá trị hợp lý tối thiểu 25%
        "min_required_irr": 0.10,               # Tỷ suất sinh lời kỳ vọng tối thiểu 10%/năm
        "max_acceptable_pe": 15.0,              # Giới hạn trần P/E mục tiêu chấp nhận
        "max_acceptable_pb": 1.6,               # Giới hạn trần P/B mục tiêu cho tài chính
        "max_acceptable_de": 1.5,               # Giới hạn D/E (chỉ áp dụng phi tài chính)
        "forecast_scenario": "conservative",    # Kịch bản ưu tiên đối chiếu
    },
    "balanced": {
        "name": "Cân bằng",
        "description": "Cân đối giữa chất lượng doanh nghiệp, triển vọng tăng trưởng và định giá.",
        "required_margin_of_safety": 0.15,     # Biên an toàn 15%
        "min_required_irr": 0.13,               # Tỷ suất sinh lời kỳ vọng 13%/năm
        "max_acceptable_pe": 18.0,
        "max_acceptable_pb": 2.0,
        "max_acceptable_de": 2.5,
        "forecast_scenario": "base",
    },
    "growth": {
        "name": "Tăng trưởng",
        "description": "Ưu tiên tốc độ bứt phá lợi nhuận và mở rộng quy mô, chấp nhận biến động thị giá.",
        "required_margin_of_safety": 0.08,     # Biên an toàn tối thiểu 8%
        "min_required_irr": 0.16,               # Kỳ vọng sinh lời tối thiểu 16%/năm
        "max_acceptable_pe": 22.0,
        "max_acceptable_pb": 2.5,
        "max_acceptable_de": 3.5,
        "forecast_scenario": "optimistic",
    }
}