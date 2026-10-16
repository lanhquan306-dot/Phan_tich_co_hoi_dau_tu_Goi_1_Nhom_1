"""Cấu hình toàn dự án, đọc từ file .env (xem .env.example).

Mọi ngưỡng của chiến lược nằm trong `StrategyConfig` để có thể chỉnh mà
không phải sửa code.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _bool(name: str, default: bool) -> bool:
    return _str(name, "true" if default else "false").lower() in {"1", "true", "yes", "on"}


def _csv(name: str, default: str = "") -> list[str]:
    """'FPT, vnm' -> ['FPT', 'VNM']."""
    return [x.strip().upper() for x in os.getenv(name, default).split(",") if x.strip()]


# ---------------------------------------------------------------- Telegram + Groq
TELEGRAM_BOT_TOKEN = _str("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = _str("GROQ_API_KEY")
GROQ_MODEL = _str("GROQ_MODEL", "openai/gpt-oss-20b")
GROQ_MODEL_FAST = _str("GROQ_MODEL_FAST", "openai/gpt-oss-20b")
GROQ_MODEL_VALUATION = _str("GROQ_MODEL_VALUATION", "openai/gpt-oss-120b")
GROQ_FALLBACK_MODEL = _str("GROQ_FALLBACK_MODEL", "openai/gpt-oss-20b")
GROQ_TIMEOUT_MS = max(3_000, _int("GROQ_TIMEOUT_MS", 15_000))
GROQ_RETRY_ATTEMPTS = max(0, _int("GROQ_RETRY_ATTEMPTS", 1))


# ---------------------------------------------------------------- VNStock
def _has_vnstock_api_key() -> bool:
    """Có API key Community (biến môi trường hoặc ~/.vnstock/api_key.json) hay không."""
    if _str("VNSTOCK_API_KEY"):
        return True
    try:
        key_file = Path.home() / ".vnstock" / "api_key.json"
        if key_file.exists():
            data = json.loads(key_file.read_text(encoding="utf-8-sig"))
            return bool(str(data.get("api_key", "")).strip())
    except (OSError, ValueError):
        pass
    return False


# Quota VNStock: khách 20 request/phút (để 16 cho an toàn), có key 60/phút (để 50).
LOCAL_VNSTOCK_BUDGET_PER_MIN = max(
    5, _int("LOCAL_VNSTOCK_BUDGET_PER_MIN", 50 if _has_vnstock_api_key() else 16)
)

# Thứ tự nguồn dữ liệu: VCI trước, MAS (chỉ khi có gói vnstock_data), KBS cuối.
PRICE_HISTORY_SOURCES = _csv("PRICE_HISTORY_SOURCES", "TCBS,VND")
PRICE_BOARD_SOURCES = _csv("PRICE_BOARD_SOURCES", "TCBS,VND")
INDEX_HISTORY_SOURCES = _csv("INDEX_HISTORY_SOURCES", "TCBS,VND")

# Thời gian sống của cache trong RAM (giây).
QUOTE_CACHE_SECONDS = max(0, _int("QUOTE_CACHE_SECONDS", 3))
HISTORY_CACHE_SECONDS = max(60, _int("HISTORY_CACHE_SECONDS", 900))
INDEX_CACHE_SECONDS = max(60, _int("INDEX_CACHE_SECONDS", 900))
FUNDAMENTAL_CACHE_SECONDS = max(600, _int("FUNDAMENTAL_CACHE_SECONDS", 43_200))
LISTING_CACHE_SECONDS = max(600, _int("LISTING_CACHE_SECONDS", 86_400))


# ---------------------------------------------------------------- Bot
ALERT_SECONDS = max(300, _int("ALERT_SECONDS", 600))
TOP_SYMBOLS = _csv("TOP_SYMBOLS", "FPT,VNM,HPG")
BLOCKED_SYMBOLS = set(_csv("BLOCKED_SYMBOLS"))
# Ngân hàng / chứng khoán / bảo hiểm: miễn điều kiện nợ vay/VCSH của bộ lọc cứng.
FINANCIAL_SYMBOLS = set(_csv(
    "FINANCIAL_SYMBOLS",
    "VCB,BID,CTG,TCB,MBB,ACB,STB,HDB,VPB,LPB,SHB,SSB,VIB,OCB,MSB,EIB,TPB,"
    "SSI,VND,HCM,VCI,FTS,BSI,MBS,BVS,VDS,BVH",
))


# ---------------------------------------------------------------- Biểu đồ + dashboard
CHART_DEFAULT_PERIOD = _str("CHART_DEFAULT_PERIOD", "6M").upper() or "6M"
DASHBOARD_ENABLED = _bool("DASHBOARD_ENABLED", True)
DASHBOARD_HOST = _str("DASHBOARD_HOST", "127.0.0.1") or "127.0.0.1"
DASHBOARD_PORT = _int("DASHBOARD_PORT", 8050)
# Link public mà Telegram mở được (VD https://abc.trycloudflare.com).
# Để trống: nút "Mở biểu đồ tương tác" gửi file HTML thay vì mở link.
DASHBOARD_PUBLIC_URL = _str("DASHBOARD_PUBLIC_URL").rstrip("/")


# ---------------------------------------------------------------- Chiến lược
@dataclass(frozen=True)
class StrategyConfig:
    # Cổng thị trường (VN-Index)
    market_ma: int = _int("MARKET_MA", 50)
    dist_window: int = _int("DIST_WINDOW", 25)             # số phiên để đếm phiên phân phối
    dist_threshold: int = _int("DIST_THRESHOLD", 4)        # >= ngưỡng này -> PRESSURE
    dist_drop_pct: float = _float("DIST_DROP_PCT", 0.2)    # phiên phân phối: giảm >= 0.2% và KL tăng
    buy_score_uptrend: float = _float("BUY_SCORE_UPTREND", 60)
    buy_score_pressure: float = _float("BUY_SCORE_PRESSURE", 75)
    position_weight: float = _float("POSITION_WEIGHT", 0.10)   # tỷ trọng chuẩn mỗi mã
    pressure_weight_factor: float = _float("PRESSURE_WEIGHT_FACTOR", 0.5)

    # Bộ lọc cứng
    min_adv_value: float = _float("MIN_ADV_VALUE", 10_000_000_000)   # GTGD TB 20 phiên (VND)
    min_history: int = _int("MIN_HISTORY", 253)
    max_debt_equity: float = _float("MAX_DEBT_EQUITY", 1.5)

    # T2.1: Delta ROC63 so với VN-Index
    t21_roc_n: int = _int("T21_ROC_N", 63)
    t21_delta_full: float = _float("T21_DELTA_FULL", 15)
    t21_delta_partial: float = _float("T21_DELTA_PARTIAL", 5)

    # T3: breakout / khối lượng / RSI
    pivot_n: int = _int("PIVOT_N", 30)
    breakout_window: int = _int("BREAKOUT_WINDOW", 10)
    buy_zone: float = _float("BUY_ZONE", 0.07)
    vol_score_low: float = _float("VOL_SCORE_LOW", 1.20)
    vol_score_full: float = _float("VOL_SCORE_FULL", 1.50)
    rsi_min: float = _float("RSI_MIN", 50)
    rsi_max: float = _float("RSI_MAX", 85)

    # Thoát lệnh
    exit_low_n: int = _int("EXIT_LOW_N", 20)
    brk_vol_mult: float = _float("BRK_VOL_MULT", 1.30)
    brk_rsi: float = _float("BRK_RSI", 45)
    stop: float = _float("STOP", 0.07)
    trail_trigger: float = _float("TRAIL_TRIGGER", 0.15)
    trail: float = _float("TRAIL", 0.10)


STRATEGY = StrategyConfig()


# ---------------------------------------------------------------- Backtest
# Danh sách 15 mã cố định theo kế hoạch Backtest (không đổi theo kết quả).
BACKTEST_DEFAULT_SYMBOLS = "FPT,HPG,MWG,VNM,VCB,MBB,TCB,SSI,HCM,VHM,VIC,GAS,PLX,GMD,PNJ"


@dataclass(frozen=True)
class BacktestConfig:
    # Khoảng kiểm định. Để trống: END = hôm nay, START = END lùi `default_months` tháng.
    start: str = _str("BACKTEST_START")
    end: str = _str("BACKTEST_END")
    default_months: int = max(1, _int("BACKTEST_MONTHS", 6))
    symbols: tuple = tuple(_csv("BACKTEST_SYMBOLS", BACKTEST_DEFAULT_SYMBOLS))

    initial_capital: float = _float("INITIAL_CAPITAL", 100_000_000)
    max_positions: int = max(1, _int("MAX_POSITIONS", 5))
    use_margin: bool = _bool("USE_MARGIN", False)
    sell_tax: float = _float("SELL_TAX", 0.001)                 # 0,1% giá trị bán
    transaction_fee: float = _float("TRANSACTION_FEE", 0.0015)  # phí môi giới mỗi chiều
    # PRESSURE: giảm tỷ trọng theo cổng thị trường giống bot (weight_factor = 0.5).
    apply_weight_factor: bool = _bool("BACKTEST_APPLY_WEIGHT_FACTOR", True)

    lot_size: int = max(1, _int("BACKTEST_LOT_SIZE", 100))      # lô chẵn HOSE/HNX
    settlement_days: int = max(0, _int("BACKTEST_SETTLEMENT_DAYS", 2))  # T+2: chưa về thì chưa bán
    # BCTC quý chỉ được dùng sau ngày kết thúc quý + số ngày này (hạn công bố BCTC hợp nhất quý).
    fund_lag_days: int = max(0, _int("BACKTEST_FUND_LAG_DAYS", 45))
    # Số ngày lịch lịch sử mà bot live nhìn thấy (stock_service lấy 620 ngày) -> backtest dùng y hệt.
    lookback_days: int = max(400, _int("BACKTEST_LOOKBACK_DAYS", 620))
    # Cuối kỳ còn vị thế: bán giả định tại giá đóng cửa phiên cuối (có phí + thuế) để chốt Trade Log.
    close_at_end: bool = _bool("BACKTEST_CLOSE_AT_END", True)
    cache_hours: float = _float("BACKTEST_CACHE_HOURS", 12)


BACKTEST = BacktestConfig()


# ---------------------------------------------------------------- Kiểm định độ chính xác tín hiệu
@dataclass(frozen=True)
class ValidationConfig:
    # Chấm đúng/sai bằng giá sau `horizon` phiên (5, 10 hoặc 20).
    horizon: int = _int("VALIDATION_HORIZON", 20)
    # Biên "đi ngang" (%): |thay đổi| <= band thì coi là ĐI NGANG. 0 = chỉ xét tăng/giảm.
    flat_band: float = _float("VALIDATION_FLAT_BAND", 0.0)
    # CHỜ mà sau đó giá tăng hơn mức này (%) -> "bỏ lỡ".
    missed_gain: float = _float("VALIDATION_MISSED_GAIN", 10.0)
    # Số tháng quá khứ mặc định khi kiểm định.
    months: int = max(1, _int("VALIDATION_MONTHS", 6))


VALIDATION = ValidationConfig()

# ---------------------------------------------------------------- Web investment analysis: financial company subtypes
# Used to select appropriate metrics. D/E is not scored for these financial firms.
BANK_SYMBOLS = set(_csv(
    "BANK_SYMBOLS",
    "VCB,BID,CTG,TCB,MBB,ACB,STB,HDB,VPB,LPB,SHB,SSB,VIB,OCB,MSB,EIB,TPB,NAB,BVB,BAB,KLB,PGB,SGB,VAB"
))
SECURITIES_SYMBOLS = set(_csv(
    "SECURITIES_SYMBOLS",
    "SSI,VND,HCM,VCI,FTS,BSI,MBS,BVS,VDS,CTS,AGR,ORS,SHS,VIX,TVS,APG,DSC"
))
INSURANCE_SYMBOLS = set(_csv(
    "INSURANCE_SYMBOLS",
    "BVH,BMI,BIC,MIG,PGI,PTI,PVI,VNR"
))
