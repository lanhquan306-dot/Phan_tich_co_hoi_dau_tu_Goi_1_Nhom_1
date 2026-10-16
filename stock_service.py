"""Tầng dữ liệu: lấy, chuẩn hóa và cache dữ liệu VNStock rồi gọi strategy.py để chấm điểm.

Module này không dùng AI. Mọi con số (giá, chỉ báo, BCTC) và tín hiệu
BUY/HOLD/SELL đều được tính theo quy tắc cố định.

Nội dung:
    1. Giới hạn tần suất gọi VNStock + cache trong RAM
    2. Chuẩn hóa dữ liệu (giá, cột OHLCV)
    3. Danh sách mã và nhận diện mã trong câu chat
    4. Lịch sử giá, VN-Index, bảng giá
    5. Báo cáo tài chính: EPS YoY, ROE TTM, nợ vay/VCSH
    6. analyze_symbol(): ghép tất cả và chấm điểm một mã
"""
from __future__ import annotations

import difflib
import re
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd

import source_adapter as sa
import strategy
from config import (
    FINANCIAL_SYMBOLS,
    FUNDAMENTAL_CACHE_SECONDS,
    HISTORY_CACHE_SECONDS,
    INDEX_CACHE_SECONDS,
    INDEX_HISTORY_SOURCES,
    LISTING_CACHE_SECONDS,
    LOCAL_VNSTOCK_BUDGET_PER_MIN,
    PRICE_BOARD_SOURCES,
    PRICE_HISTORY_SOURCES,
    QUOTE_CACHE_SECONDS,
    STRATEGY,
)
from utils import normalize_text, to_float

TZ = ZoneInfo("Asia/Ho_Chi_Minh")

# Từ viết hoa hay gặp trong câu hỏi nhưng không phải mã cổ phiếu.
NON_TICKER_WORDS = {
    "RSI", "EMA", "SMA", "MACD", "EPS", "ROE", "ROA", "NAV", "BUY", "SELL", "HOLD", "YOY",
    "CAGR", "ADX", "EOD", "TTM", "MA20", "MA50", "MA150", "VNI", "VNINDEX", "HOM", "NAY",
}


class DataUnavailable(RuntimeError):
    """Không lấy được dữ liệu cần thiết."""


class VnstockRateLimited(DataUnavailable):
    """VNStock báo vượt quota; bot phải tiếp tục chạy chứ không được tắt."""


# =====================================================================
# 1. Giới hạn tần suất + cache
# =====================================================================
class RateLimiter:
    """Cửa sổ trượt 60 giây: tối đa `limit` lần gọi mỗi phút, dùng chung mọi thread."""

    def __init__(self, limit_per_minute: int):
        self.limit = max(1, int(limit_per_minute))
        self.calls: deque[float] = deque()
        self.lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self.lock:
                now = time.time()
                while self.calls and now - self.calls[0] >= 60:
                    self.calls.popleft()
                if len(self.calls) < self.limit:
                    self.calls.append(now)
                    return
                wait = max(0.25, 60 - (now - self.calls[0]) + 0.2)
            time.sleep(wait)


_limiter = RateLimiter(LOCAL_VNSTOCK_BUDGET_PER_MIN)


def vn_call(func, *args, **kwargs):
    """Gọi một hàm VNStock sau khi xin phép rate limiter.

    VNStock có thể ném SystemExit khi vượt quota; nếu không bắt riêng thì cả
    Telegram bot sẽ tắt. Lỗi quota được đổi thành VnstockRateLimited.
    """
    _limiter.acquire()
    try:
        return func(*args, **kwargs)
    except SystemExit as exc:
        raise VnstockRateLimited("VNStock đang chạm giới hạn API. Thử lại sau khoảng 1 phút.") from exc
    except Exception as exc:
        msg = str(exc).lower()
        if "rate limit" in msg or "quota" in msg:
            raise VnstockRateLimited("VNStock đang chạm giới hạn API. Thử lại sau khoảng 1 phút.") from exc
        raise


class TTLCache:
    """Cache trong RAM có thời hạn, an toàn đa luồng. Trả về bản sao để nơi gọi không sửa nhầm cache."""

    def __init__(self):
        self._items: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()

    def get(self, key: str, ttl: float, allow_expired: bool = False):
        with self._lock:
            item = self._items.get(key)
        if not item:
            return None
        ts, value = item
        return self._clone(value) if allow_expired or time.time() - ts <= ttl else None

    def set(self, key: str, value) -> None:
        with self._lock:
            self._items[key] = (time.time(), self._clone(value))

    @staticmethod
    def _clone(value):
        if isinstance(value, pd.DataFrame):
            return value.copy()
        if isinstance(value, (dict, list)):
            return type(value)(value)
        return value


_cache = TTLCache()


class _InFlightCall:
    """Một loader đang chạy cho một cache key.

    Khi nhiều thread cùng cần đúng một dữ liệu (ví dụ BCTC/ratio của cùng mã),
    chỉ thread đầu tiên gọi nguồn mạng. Các thread còn lại chờ kết quả đó thay
    vì tạo thêm request trùng lặp. Điều này đặc biệt quan trọng khi dashboard
    chạy các phần phân tích song song.
    """

    def __init__(self):
        self.event = threading.Event()
        self.result = None
        self.error: Exception | None = None


_inflight_calls: dict[str, _InFlightCall] = {}
_inflight_lock = threading.Lock()


def cached_call(key: str, ttl: float, loader):
    """Trả về (giá_trị, lấy_từ_cache, là_cache_hết_hạn).

    Nếu loader lỗi mà cache cũ còn, dùng tạm cache cũ (stale) thay vì báo lỗi.
    """
    cached = _cache.get(key, ttl)
    if cached is not None:
        return cached, True, False

    # Single-flight: gom các request đồng thời cho cùng một key thành 1 loader.
    with _inflight_lock:
        flight = _inflight_calls.get(key)
        if flight is None:
            flight = _InFlightCall()
            _inflight_calls[key] = flight
            owner = True
        else:
            owner = False

    if not owner:
        flight.event.wait()
        if flight.error is not None:
            stale = _cache.get(key, ttl, allow_expired=True)
            if stale is not None:
                return stale, True, True
            raise flight.error
        # Trả bản sao giống semantics của TTLCache để caller không sửa cache.
        value, _from_cache, stale_flag = flight.result
        return _cache._clone(value), True, bool(stale_flag)

    try:
        try:
            value = loader()
        except Exception as exc:
            stale = _cache.get(key, ttl, allow_expired=True)
            if stale is not None:
                flight.result = (stale, True, True)
                return stale, True, True
            flight.error = exc
            raise

        _cache.set(key, value)
        flight.result = (value, False, False)
        return value, False, False
    finally:
        flight.event.set()
        with _inflight_lock:
            _inflight_calls.pop(key, None)


def now_vn() -> datetime:
    return datetime.now(TZ)


def is_market_open_now() -> bool:
    """Phiên HOSE/HNX theo đồng hồ (9:00-11:30, 13:00-15:00, thứ 2-6). Không tốn API."""
    now = now_vn()
    if now.weekday() >= 5:
        return False
    t = now.time()
    return dtime(9, 0) <= t <= dtime(11, 30) or dtime(13, 0) <= t <= dtime(15, 0)


# =====================================================================
# 2. Chuẩn hóa dữ liệu
# =====================================================================
def normalize_equity_price(value):
    """Một số nguồn trả giá theo nghìn đồng (71.7): giá < 1000 được đổi sang VND."""
    x = to_float(value)
    if x is None:
        return None
    return x * 1000.0 if 0 < abs(x) < 1000 else x


def pick(mapping: dict | None, names, price: bool = False):
    """Lấy giá trị số đầu tiên có trong mapping theo danh sách tên cột ứng viên."""
    for name in names:
        x = to_float((mapping or {}).get(name))
        if x is not None:
            return normalize_equity_price(x) if price else x
    return None


def flatten_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Cột MultiIndex ('match', 'match_price') -> 'match_match_price'."""
    x = df.copy()
    if isinstance(x.columns, pd.MultiIndex):
        x.columns = ["_".join(str(v) for v in col if str(v) != "nan").strip("_") for col in x.columns]
    else:
        x.columns = [str(c) for c in x.columns]
    return x


_OHLCV_ALIASES = {
    "time": ["time", "date", "trading_date", "tradingdate"],
    "open": ["open", "open_price", "openprice"],
    "high": ["high", "high_price", "highprice", "highest"],
    "low": ["low", "low_price", "lowprice", "lowest"],
    "close": ["close", "close_price", "closeprice", "match_price", "matchprice"],
    "volume": ["volume", "total_volume", "totalvolume", "matched_volume", "matchedvolume"],
}


def normalize_ohlcv(df, equity: bool = True) -> pd.DataFrame:
    """Đưa dữ liệu mọi nguồn về đúng 6 cột time/open/high/low/close/volume, sắp theo thời gian."""
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=list(_OHLCV_ALIASES))

    x = flatten_columns(pd.DataFrame(df))
    compact = {normalize_text(c).replace(" ", ""): c for c in x.columns}
    rename = {}
    for target, names in _OHLCV_ALIASES.items():
        for name in names:
            original = compact.get(normalize_text(name).replace(" ", ""))
            if original is not None:
                rename[original] = target
                break
    x = x.rename(columns=rename)
    missing = [c for c in _OHLCV_ALIASES if c not in x.columns]
    if missing:
        raise DataUnavailable(f"OHLCV thiếu cột {missing}; columns={list(x.columns)}")

    x["time"] = pd.to_datetime(x["time"], errors="coerce")
    for c in ("open", "high", "low", "close", "volume"):
        x[c] = pd.to_numeric(x[c], errors="coerce")
    if equity:
        for c in ("open", "high", "low", "close"):
            x[c] = x[c].map(normalize_equity_price)

    return (x.dropna(subset=["time", "close"])
             .sort_values("time")
             .drop_duplicates(subset=["time"], keep="last")
             .reset_index(drop=True))


def bar_date(df) -> str | None:
    """Ngày của nến cuối, dạng dd/mm/yyyy."""
    if df is None or df.empty:
        return None
    return pd.Timestamp(df.iloc[-1]["time"]).strftime("%d/%m/%Y")


def confirmed_bars(df):
    """Bỏ nến hôm nay nếu phiên còn đang chạy: chiến lược chỉ tính trên nến đã đóng."""
    if df is None or df.empty:
        return df
    if is_market_open_now() and pd.Timestamp(df.iloc[-1]["time"]).date() == now_vn().date():
        return df.iloc[:-1].copy()
    return df


# =====================================================================
# 3. Danh sách mã và nhận diện mã
# =====================================================================
def _load_listing_map() -> dict[str, dict]:
    """{mã: {symbol, exchange, name, industry, all_names}} cho các mã HOSE/HNX."""
    result = vn_call(sa.fetch_listing)
    df = flatten_columns(pd.DataFrame(result.data))
    if df.empty:
        raise DataUnavailable("Không tải được danh sách mã cổ phiếu.")

    def column(*names):
        return [c for c in df.columns if normalize_text(c) in names]

    symbol_cols = column("symbol", "ticker")
    if not symbol_cols:
        raise DataUnavailable(f"Danh sách mã không có cột symbol. Columns={list(df.columns)}")
    exchange_cols = column("exchange", "san")
    name_cols = column("organ name", "organ short name", "company name", "name")

    # Ưu tiên lấy ngành chính (cấp 2) chuẩn BCTC để đồng bộ với Code N
    industry_cols = column(
        "icb name2", "icb name 2", "icb_name2",
        "icb name3", "icb name 3", "icb_name3",
        "icb name", "icb_name",
        "industry name", "industry", "sector", "sector name"
    )

    out: dict[str, dict] = {}
    for _, row in df.iterrows():
        symbol = str(row[symbol_cols[0]]).strip().upper()
        if not symbol or symbol == "NAN":
            continue
        exchange = str(row.get(exchange_cols[0], "") or "").upper().replace("HSX", "HOSE") if exchange_cols else ""
        if exchange and exchange not in {"HOSE", "HNX"}:
            continue
        names = [str(row.get(c, "") or "").strip() for c in name_cols]
        names = [n for n in names if n and n.lower() != "nan"]

        # Trích xuất nhãn ngành từ danh sách cột có sẵn
        industry = ""
        for icol in industry_cols:
            val = str(row.get(icol, "") or "").strip()
            if val and val.lower() not in {"nan", "none", ""}:
                industry = val
                break

        out[symbol] = {
            "symbol": symbol,
            "exchange": exchange or "UNKNOWN",
            "name": names[0] if names else "",
            "industry": industry,
            "all_names": names,
        }
    return out


def listing_map() -> dict[str, dict]:
    value, _, _ = cached_call("listing", LISTING_CACHE_SECONDS, _load_listing_map)
    return value


def _safe_listing() -> dict[str, dict]:
    try:
        return listing_map()
    except Exception:
        return {}


def _ticker_tokens(text: str, min_len: int) -> list[str]:
    tokens = re.findall(rf"\b[A-Z][A-Z0-9]{{{min_len - 1},5}}\b", str(text or "").upper())
    return [t for t in tokens if t not in NON_TICKER_WORDS]


def resolve_symbols(text: str, max_symbols: int = 5) -> list[str]:
    """Mọi mã HOSE/HNX được nhắc trong câu, theo thứ tự xuất hiện.

    Cố ý thận trọng: chỉ nhận token có trong danh sách niêm yết, hoặc tên công ty
    (VD 'Vietcombank'). Không tải được danh sách thì trả [] thay vì đoán bừa.
    """
    listing = _safe_listing()
    if not listing:
        return []
    limit = max(1, int(max_symbols))
    out: list[str] = []
    for token in _ticker_tokens(text, 2):
        if token in listing and token not in out:
            out.append(token)
            if len(out) >= limit:
                return out
    ntext = normalize_text(text)
    for symbol, meta in listing.items():
        if symbol not in out and any(
            len(n) >= 4 and n in ntext for n in map(normalize_text, meta.get("all_names") or [])
        ):
            out.append(symbol)
            if len(out) >= limit:
                break
    return out


def resolve_symbol(text: str) -> dict:
    """Nhận diện MỘT mã từ tham số lệnh (VD /signal fpt).

    Trả về một trong ba dạng:
        {"status": "ok", "symbol": "VNM"}
        {"status": "suggest", "typed": "VNN", "suggestions": ["VNM", "VND"]}
        {"status": "none"}
    """
    listing = _safe_listing()
    tokens = _ticker_tokens(text, 3)
    if not listing:
        # Không tải được danh sách: vẫn cho qua token trông giống mã.
        return {"status": "ok", "symbol": tokens[0], "unvalidated": True} if tokens else {"status": "none"}

    for token in tokens:
        if token in listing:
            return {"status": "ok", "symbol": token}

    ntext = normalize_text(text)
    name_hits = sorted({
        symbol for symbol, meta in listing.items()
        for n in map(normalize_text, meta.get("all_names") or [])
        if len(n) >= 5 and (n in ntext or ntext in n)
    })
    if len(name_hits) == 1:
        return {"status": "ok", "symbol": name_hits[0]}

    if tokens:
        close = difflib.get_close_matches(tokens[0], list(listing), n=3, cutoff=0.55)
        return {"status": "suggest", "typed": tokens[0], "suggestions": close}
    return {"status": "none"}

def _fetch_company_profile_desc(symbol: str) -> str:
    """Tự động đọc mô tả hoạt động kinh doanh từ dữ liệu công ty (VCI / VNStock)."""
    try:
        from vnstock.explorer.vci.company import Company as VCICompany
        comp = vn_call(VCICompany, symbol)
        df_prof = vn_call(comp.profile)
        if df_prof is not None and not df_prof.empty:
            row = df_prof.iloc[0].to_dict()
            desc = row.get("business_model") or row.get("main_service") or row.get("history_dev") or ""
            if desc:
                return str(desc).strip()
    except Exception:
        pass

    return ""


def get_company_profile_desc(symbol: str) -> str:
    """Lấy mô tả hoạt động qua TTLCache (lưu 24 giờ trong RAM) để đạt tốc độ cao nhất."""
    desc, _, _ = cached_call(f"profile_desc:{symbol.upper()}", 86400, lambda: _fetch_company_profile_desc(symbol.upper()))
    return desc or ""

# =====================================================================
# 4. Lịch sử giá, VN-Index, bảng giá
# =====================================================================
def _fetch_ohlcv(symbol: str, sources, calendar_days: int, equity: bool,
                 start: str | None = None, end: str | None = None) -> pd.DataFrame:
    result = vn_call(sa.fetch_history, symbol, sources=sources, start=start,
                     end=end or now_vn().date().isoformat(), calendar_days=calendar_days, interval="1D")
    out = normalize_ohlcv(result.data, equity=equity)
    out.attrs["source"] = result.source
    return out


def fetch_history_range(symbol: str, start: str, end: str, *, index: bool = False) -> pd.DataFrame:
    """OHLCV trong khoảng [start, end] (ISO). Dùng cho backtest; không đi qua cache RAM ngắn hạn."""
    sources = INDEX_HISTORY_SOURCES if index else PRICE_HISTORY_SOURCES
    return _fetch_ohlcv(symbol.upper(), sources, 0, equity=not index, start=start, end=end)


def _load_history(symbol: str) -> pd.DataFrame:
    # 620 ngày lịch thường > 400 phiên: đủ cho MA150, đỉnh 52 tuần và bộ lọc lịch sử.
    out = _fetch_ohlcv(symbol, ["TCBS", "VND"], 620, equity=True)
    if len(out) < STRATEGY.min_history:
        # Nguồn trả thiếu phiên: thử lại một cửa sổ dài hơn.
        longer = _fetch_ohlcv(symbol, PRICE_HISTORY_SOURCES, 1500, equity=True)
        if len(longer) > len(out):
            return longer
    return out


def get_history(symbol: str):
    """(DataFrame OHLCV, lấy_từ_cache, cache_hết_hạn) của một mã."""
    symbol = symbol.upper()
    return cached_call(f"history:{symbol}", HISTORY_CACHE_SECONDS, lambda: _load_history(symbol))


def get_index_history():
    """(DataFrame OHLCV của VN-Index, lấy_từ_cache, cache_hết_hạn)."""
    return cached_call("index:VNINDEX", INDEX_CACHE_SECONDS,
                       lambda: _fetch_ohlcv("VNINDEX", INDEX_HISTORY_SOURCES, 620, equity=False))


def _quote_row(df) -> dict:
    """Dòng đầu bảng giá; thêm bản sao không tiền tố cho cột 'match_price' -> 'price'..."""
    x = pd.DataFrame(df) if df is not None else pd.DataFrame()
    if x.empty:
        return {}
    row = flatten_columns(x).iloc[0].to_dict()
    for key in list(row):
        lk = str(key).lower()
        for prefix in ("listing_", "match_", "bid_ask_", "bidask_"):
            if lk.startswith(prefix) and len(lk) > len(prefix):
                row.setdefault(lk[len(prefix):], row[key])
                break
    return row


def _empty_quote(symbol: str, **extra) -> dict:
    q = dict.fromkeys(("price", "reference", "open", "high", "low", "volume", "percent_change"))
    return {"symbol": symbol, **q, "retrieved_at": now_vn().isoformat(timespec="seconds"), **extra}


def _load_quote(symbol: str) -> dict:
    try:
        result = vn_call(sa.fetch_single_quote, symbol, sources=["TCBS", "VND"])
        if result and getattr(result, "data", None) is not None:
            row = _quote_row(result.data)
            price = pick(row, ["match_price", "price", "last_price", "close", "current_price", "matched_price"], price=True)
            ref = pick(row, ["reference_price", "reference", "ref_price", "previous_close", "prior_close"], price=True)
            pct = pick(row, ["percent_change", "percent_price_change", "change_percent", "pct_change"])
            if pct is None and price is not None and ref:
                pct = (price / ref - 1.0) * 100
            return _empty_quote(
                symbol,
                price=price,
                reference=ref,
                open=pick(row, ["open", "open_price"], price=True),
                high=pick(row, ["high", "highest", "high_price"], price=True),
                low=pick(row, ["low", "lowest", "low_price"], price=True),
                volume=pick(row, ["volume", "total_volume", "volume_accumulated", "accumulated_volume", "match_volume"]),
                percent_change=pct,
                data_source=result.source,
            )
    except Exception:
        pass
    return _empty_quote(symbol)


def get_quote(symbol: str):
    """(quote, lấy_từ_cache, cache_hết_hạn). Cache chỉ vài giây vì là giá khớp lệnh."""
    symbol = symbol.upper()
    return cached_call(f"quote:{symbol}", QUOTE_CACHE_SECONDS, lambda: _load_quote(symbol))


def live_or_history_quote(symbol: str, hist: pd.DataFrame) -> tuple[dict, list[str]]:
    """Giá hiển thị cho phần phân tích.

    Ngoài giờ giao dịch dùng luôn nến EOD cuối (không tốn thêm API). Trong giờ
    giao dịch lấy bảng giá live; trường nào live thiếu thì bù từ nến cuối.
    """
    warnings: list[str] = []
    has_hist = hist is not None and not hist.empty
    if not is_market_open_now() and has_hist:
        q = _empty_quote(symbol, data_source=hist.attrs.get("source", "EOD_HISTORY"), quote_mode="EOD_HISTORY")
    else:
        try:
            q, _, stale = get_quote(symbol)
            if stale:
                warnings.append("Bảng giá live đang dùng cache cũ do nguồn tạm lỗi.")
        except Exception as exc:
            q = _empty_quote(symbol)
            warnings.append(f"Không lấy được quote live: {type(exc).__name__}")

    if has_hist:
        last = hist.iloc[-1]
        for field in ("open", "high", "low", "volume"):
            if q[field] is None:
                q[field] = to_float(last[field])
        if q["price"] is None:
            q["price"] = to_float(last["close"])
        if q["reference"] is None and len(hist) >= 2:
            q["reference"] = to_float(hist.iloc[-2]["close"])
        if q["percent_change"] is None and q["price"] is not None and q["reference"]:
            q["percent_change"] = (q["price"] / q["reference"] - 1) * 100
    return q, warnings


def get_market_gate():
    """(VN-Index đã bỏ nến đang chạy, trạng thái cổng thị trường, cache_hết_hạn)."""
    idx, _, stale = get_index_history()
    idx = confirmed_bars(idx)
    return idx, strategy.market_gate(idx), stale


# =====================================================================
# 5. Báo cáo tài chính
# =====================================================================
# Tên dòng BCTC (đã bỏ dấu) cho từng chỉ tiêu, ưu tiên theo thứ tự.
_EPS_PHRASES = [
    "lai co ban tren co phieu", "basic earnings per share", "earnings per share",
    "thu nhap tren moi co phieu", "eps",
]
_NPAT_PARENT_PHRASES = [
    "loi nhuan sau thue cua co dong cong ty me", "loi nhuan sau thue cua co dong cua cong ty me",
    "loi nhuan sau thue cua cong ty me", "lnst cua co dong cong ty me", "co dong cua cong ty me",
    "co dong cong ty me", "attributable to parent company", "attributable to parent",
    "profit after tax of parent", "net profit attributable to parent",
    "attributable to shareholders of the parent", "net profit after tax attributable to parent",
]
_NPAT_TOTAL_PHRASES = [
    "loi nhuan sau thue thu nhap doanh nghiep", "loi nhuan sau thue", "net profit after tax",
    "profit after tax", "net profit", "net income",
]
_EQUITY_PHRASES = [
    "von chu so huu", "owners equity", "owner s equity", "shareholders equity", "total equity",
]
_ST_DEBT_PHRASES = [
    "vay va no thue tai chinh ngan han", "vay ngan han", "vay va no ngan han",
    "short term borrowings and finance lease liabilities", "short term borrowings", "short term loans",
]
_LT_DEBT_PHRASES = [
    "vay va no thue tai chinh dai han", "vay dai han", "vay va no dai han",
    "long term borrowings and finance lease liabilities", "long term borrowings", "long term loans",
]


def _parse_quarter(label) -> tuple[int, int] | None:
    """'2026-Q2', 'Q2/2026', 'Quý 2 2026'... -> (2026, 2)."""
    t = normalize_text(label).upper()
    m = re.search(r"(\d{4})\s*Q\s*([1-4])\b", t)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"\b(?:Q|QUY)\s*([1-4])\s*(\d{4})\b", t)
    return (int(m.group(2)), int(m.group(1))) if m else None


def _previous_quarter(year: int, quarter: int) -> tuple[int, int]:
    return (year, quarter - 1) if quarter > 1 else (year - 1, 4)


def _quarter_columns(df) -> dict[tuple[int, int], str]:
    """{(năm, quý): tên cột} của một bảng BCTC dạng 'kỳ nằm ở tên cột'."""
    if df is None or df.empty:
        return {}
    out = {}
    for col in df.columns:
        q = _parse_quarter(col)
        if q and q not in out:
            out[q] = col
    return out


def _find_report_row(df, phrases):
    """Tìm dòng BCTC theo tên chỉ tiêu: ưu tiên trùng khớp hoàn toàn, sau đó chứa nguyên cụm từ."""
    if df is None or df.empty:
        return None
    x = flatten_columns(df)
    label_cols = [c for c in x.columns if normalize_text(c) in {
        "item", "item en", "item id", "name", "name en", "chi tieu", "ten chi tieu", "indicator"}]
    labels = {col: x[col].astype(str).map(normalize_text) for col in label_cols}
    keys = [p for p in map(normalize_text, phrases) if p]
    for key in keys:
        for col in label_cols:
            hit = x[labels[col] == key]
            if not hit.empty:
                return hit.iloc[0]
    for key in keys:
        pattern = r"(?:^|\s)" + re.escape(key) + r"(?:\s|$)"
        for col in label_cols:
            hit = x[labels[col].str.contains(pattern, regex=True, na=False)]
            if not hit.empty:
                return hit.iloc[0]
    return None


def _yoy_growth(df, phrases) -> dict:
    """Tăng trưởng cùng kỳ (YoY) của quý mới nhất cho một chỉ tiêu.

    status: 'ok' | 'base_nonpositive' (cùng kỳ <= 0, theo quy tắc chấm 0 điểm) | 'missing'.
    """
    out = {"status": "missing", "growth": None}
    row = _find_report_row(df, phrases)
    cols = _quarter_columns(df)
    if row is None or not cols:
        return out
    year, quarter = max(cols)
    now = to_float(row.get(cols[(year, quarter)]))
    old_col = cols.get((year - 1, quarter))
    old = to_float(row.get(old_col)) if old_col else None
    if now is None or old is None:
        return out
    if old <= 0:
        out["status"] = "base_nonpositive"
    else:
        out.update(status="ok", growth=(now / old - 1) * 100)
    return out


def _compute_roe_ttm(income_q, balance_q):
    """ROE TTM (%) = tổng LNST 4 quý gần nhất / VCSH bình quân cuối kỳ của 4 quý đó.

    Ưu tiên LNST hợp nhất cho nhất quán với tổng VCSH; không có thì dùng LNST cổ đông công ty mẹ.
    """
    qi, qb = _quarter_columns(income_q), _quarter_columns(balance_q)
    if len(qi) < 4 or len(qb) < 4:
        return None
    profit_row = _find_report_row(income_q, _NPAT_TOTAL_PHRASES)
    if profit_row is None:
        profit_row = _find_report_row(income_q, _NPAT_PARENT_PHRASES)
    equity_row = _find_report_row(balance_q, _EQUITY_PHRASES)
    if profit_row is None or equity_row is None:
        return None

    period = max(qi)
    profits, equities = [], []
    for _ in range(4):
        profits.append(to_float(profit_row.get(qi[period])) if period in qi else None)
        equities.append(to_float(equity_row.get(qb[period])) if period in qb else None)
        period = _previous_quarter(*period)
    if None in profits or None in equities:
        return None
    avg_equity = sum(equities) / 4
    return sum(profits) / avg_equity * 100 if avg_equity > 0 else None


def _compute_debt_equity(balance_q):
    """Nợ vay CHỊU LÃI (ngắn + dài hạn) / VCSH của quý mới nhất."""
    cols = _quarter_columns(balance_q)
    if not cols:
        return None
    col = cols[max(cols)]
    st = _find_report_row(balance_q, _ST_DEBT_PHRASES)
    lt = _find_report_row(balance_q, _LT_DEBT_PHRASES)
    eq = _find_report_row(balance_q, _EQUITY_PHRASES)
    if eq is None or (st is None and lt is None):
        return None
    equity = to_float(eq.get(col))
    if equity is None or equity <= 0:
        return None
    debt = sum(to_float(r.get(col), 0.0) for r in (st, lt) if r is not None)
    return debt / equity


def _latest_period(*dfs) -> str | None:
    periods = [q for df in dfs for q in _quarter_columns(df)]
    if not periods:
        return None
    year, quarter = max(periods)
    return f"{year}-Q{quarter}"


def _load_fundamentals_vci(symbol: str) -> dict:
    """VCI trả toàn bộ các quý trong một request.

    Gọi hàm nội bộ `_get_financial_report` vì hàm public của vnstock chỉ trả 4 kỳ,
    không đủ để so cùng kỳ năm trước.
    """
    from vnstock.explorer.vci.financial import Finance as VCIFinance

    def report(name):
        # Mỗi worker có client riêng để tránh chia sẻ state nội bộ của VNStock
        # giữa hai thread. Constructor là local nên không tiêu thụ quota mạng.
        fin = VCIFinance(symbol, period="quarter", get_all=True, show_log=False)
        df = vn_call(fin._get_financial_report, name, period="quarter", lang="vi",
                     dropna=False, show_log=False, limit=500)
        return flatten_columns(pd.DataFrame(df)) if df is not None else pd.DataFrame()

    # Hai báo cáo độc lập, gọi song song để không cộng dồn latency mạng.
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="vci-financial") as pool:
        income_future = pool.submit(report, "income_statement")
        balance_future = pool.submit(report, "balance_sheet")
        income_q = income_future.result()
        balance_q = balance_future.result()
    return {"income_q": income_q, "balance_q": balance_q, "source": "VCI"}


def _load_fundamentals_kbs(symbol: str) -> dict:
    """KBS chỉ có 4 kỳ gần nhất nên thường thiếu cùng kỳ; chỉ dùng dự phòng."""
    from vnstock import Fundamental

    def report(method_name):
        try:
            # Tách object theo worker để parallel an toàn hơn.
            equity = Fundamental().equity(symbol)
            fn = getattr(equity, method_name)
            return flatten_columns(pd.DataFrame(vn_call(fn, period="quarter", orient="report")))
        except VnstockRateLimited:
            raise
        except Exception:
            return pd.DataFrame()

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="kbs-financial") as pool:
        income_future = pool.submit(report, "income_statement")
        balance_future = pool.submit(report, "balance_sheet")
        income_q = income_future.result()
        balance_q = balance_future.result()
    return {"income_q": income_q, "balance_q": balance_q, "source": "KBS"}


def _load_fundamentals(symbol: str) -> dict:
    """VCI nếu có ít nhất 5 quý (đủ so cùng kỳ), ngược lại dùng KBS."""
    errors = []
    try:
        tables = _load_fundamentals_vci(symbol)
        n = len(_quarter_columns(tables["income_q"]))
        if n >= 5:
            return tables
        errors.append(f"VCI chỉ có {n} quý")
    except VnstockRateLimited:
        raise
    except Exception as exc:
        errors.append(f"VCI lỗi: {type(exc).__name__}: {str(exc)[:120]}")
    tables = _load_fundamentals_kbs(symbol)
    tables["source_errors"] = errors
    return tables


def get_fundamental_tables(symbol: str):
    return cached_call(f"fund:{symbol}", FUNDAMENTAL_CACHE_SECONDS, lambda: _load_fundamentals(symbol))


def truncate_quarters(df, upto: tuple[int, int] | None):
    """Bỏ các cột quý SAU kỳ `upto` (năm, quý) -> bảng BCTC "như tại thời điểm đó".

    Dùng cho backtest để không nhìn thấy BCTC chưa công bố (tránh Data Leakage).
    """
    if upto is None or df is None or df.empty:
        return df
    keep = [c for c in df.columns if (_parse_quarter(c) is None or _parse_quarter(c) <= tuple(upto))]
    return df[keep]


def fundamental_snapshot(symbol: str) -> dict:
    """Ba số liệu BCTC mà chiến lược dùng: EPS YoY, ROE TTM, nợ vay/VCSH."""
    notes: list[str] = []
    try:
        tables, _, stale = get_fundamental_tables(symbol)
        if stale:
            notes.append("BCTC đang dùng cache cũ do nguồn tạm lỗi.")
    except VnstockRateLimited:
        raise
    except Exception as exc:
        tables = {"source": "?"}
        notes.append(f"Không tải được BCTC: {type(exc).__name__}")
    return snapshot_from_tables(symbol, tables, notes)


def snapshot_from_tables(symbol: str, tables: dict, notes: list[str] | None = None,
                         upto: tuple[int, int] | None = None) -> dict:
    """Tính EPS YoY / ROE TTM / nợ vay/VCSH từ bảng BCTC; `upto` giới hạn quý mới nhất được thấy."""
    notes = list(notes or [])
    income = truncate_quarters(tables.get("income_q", pd.DataFrame()), upto)
    balance = truncate_quarters(tables.get("balance_q", pd.DataFrame()), upto)
    source = tables.get("source", "?")
    n_quarters = len(_quarter_columns(income))
    if 0 < n_quarters < 5:
        notes.append(f"Nguồn {source} chỉ có {n_quarters} quý: không đủ so cùng kỳ năm trước.")

    # EPS YoY đo bằng tăng trưởng LNST cổ đông công ty mẹ (không bị méo khi chia cổ phiếu).
    eps = _yoy_growth(income, _NPAT_PARENT_PHRASES)
    if eps["status"] == "missing":
        eps = _yoy_growth(income, _EPS_PHRASES)
        if eps["status"] != "missing":
            notes.append("Không có dòng LNST cổ đông công ty mẹ; dùng EPS báo cáo.")

    roe = _compute_roe_ttm(income, balance)
    if roe is not None and not -100 <= roe <= 100:
        notes.append(f"ROE TTM tính ra {roe:.0f}% (bất thường) -> coi là thiếu.")
        roe = None

    debt_equity = _compute_debt_equity(balance)
    if debt_equity is not None and not 0 <= debt_equity <= 20:
        notes.append(f"Nợ vay/VCSH tính ra {debt_equity:.2f} (bất thường) -> coi là thiếu.")
        debt_equity = None

    latest = _latest_period(income, balance)
    return {
        "available": latest is not None,
        "latest_period": latest,
        "fund_source": source,
        "eps_yoy": eps["growth"],
        "eps_status": eps["status"],
        "roe": roe,
        "debt_equity": debt_equity,
        "is_financial": symbol in FINANCIAL_SYMBOLS,
        "notes": notes,
    }


# =====================================================================
# 6. Phân tích một mã
# =====================================================================
def analyze_symbol(symbol: str, position: dict | None = None) -> dict:
    """Lấy toàn bộ dữ liệu của một mã, tính chỉ báo và chấm điểm trên nến EOD đã đóng.

    Lỗi của từng nguồn con được ghi vào `warnings`/`errors` thay vì làm hỏng cả
    kết quả; riêng VnstockRateLimited được ném ra để bot báo người dùng chờ.
    """
    s = symbol.upper()
    warnings: list[str] = []
    errors: list[str] = []

    meta = {"exchange": "UNKNOWN", "name": ""}
    try:
        meta = listing_map().get(s, meta)
    except Exception as exc:
        warnings.append(f"Không kiểm tra được danh sách mã: {type(exc).__name__}")

    hist = pd.DataFrame()
    try:
        hist, _, stale = get_history(s)
        if stale:
            warnings.append("Lịch sử giá đang dùng cache cũ do nguồn tạm lỗi.")
    except VnstockRateLimited:
        raise
    except Exception as exc:
        errors.append(f"History: {exc}")
    quote, quote_warnings = live_or_history_quote(s, hist)
    warnings.extend(quote_warnings)
    confirmed = confirmed_bars(hist)

    index = pd.DataFrame()
    gate = strategy.market_gate(None)
    try:
        index, gate, stale = get_market_gate()
        if stale:
            warnings.append("VN-Index đang dùng cache cũ.")
    except VnstockRateLimited:
        raise
    except Exception as exc:
        errors.append(f"VNINDEX: {exc}")

    try:
        tech = strategy.compute_technical(confirmed, index)
    except Exception as exc:
        errors.append(f"Technical EOD: {exc}")
        tech = {"history_count": len(confirmed), "breakout": {}}

    fund = fundamental_snapshot(s)
    warnings.extend(fund["notes"])
    if tech.get("delta_roc63") is None:
        warnings.append("Không tính được Delta ROC63 (thiếu lịch sử mã hoặc VN-Index); T2.1 để UNKNOWN.")
    elif tech.get("roc_aligned") is False:
        warnings.append(f"VN-Index mới có tới {tech.get('roc_index_date')}, nến mã {tech.get('roc_stock_date')}: "
                        "Delta ROC63 dùng mốc VN-Index gần nhất.")

    price, volume = quote.get("price"), quote.get("volume")
    
    # Tự động lấy mô tả hoạt động kinh doanh từ cache/VCI
    profile_desc = get_company_profile_desc(s)

    result = {
        "symbol": s,
        "company_name": meta.get("name", ""),
        "exchange": meta.get("exchange", "UNKNOWN"),
        "industry": meta.get("industry", ""),       # <-- Thêm dòng này để truyền ngành chuẩn
        "profile": profile_desc,                   # <-- Thêm dòng này để giao diện bật khung mô tả
        "confirmed_bar_date": bar_date(confirmed),
        "index_bar_date": bar_date(index),
        "quote": quote,
        "price_source": quote.get("data_source") or confirmed.attrs.get("source"),
        "gtgd": price * volume if price is not None and volume is not None else None,
        "market": gate,
        "tech": tech,
        "fund": fund,
        "warnings": warnings,
        "errors": errors,
    }
    result.update(strategy.evaluate(s, tech, fund, gate, meta, position=position))
    return result