"""Lớp adapter gọi thư viện VNStock với thứ tự nguồn rõ ràng và cơ chế dự phòng.

- VCI, KBS: có trong gói vnstock Community (miễn phí).
- MAS: chỉ dùng được khi cài gói vnstock_data (Sponsor).

Các bản vnstock 4.x đặt class Quote/Trading/Listing ở những module khác nhau và
nhận tham số khởi tạo khác nhau, nên module này dò lần lượt các biến thể.
"""
from __future__ import annotations

import importlib
import inspect
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

DEFAULT_SOURCES = ("VCI", "MAS", "KBS")


class ProviderUnavailable(RuntimeError):
    """Nguồn dữ liệu không dùng được (thiếu gói, lỗi mạng, trả rỗng...)."""


@dataclass
class ProviderResult:
    data: pd.DataFrame
    source: str      # VCI / MAS / KBS / AUTO
    backend: str     # class thực sự đã gọi, để debug


def _source_order(value: str | Iterable[str] | None) -> list[str]:
    """Chuẩn hóa danh sách nguồn: 'vci, kbs' -> ['VCI', 'KBS'], bỏ trùng."""
    if value is None:
        items = list(DEFAULT_SOURCES)
    elif isinstance(value, str):
        items = value.split(",")
    else:
        items = list(value)
    out: list[str] = []
    for x in items:
        u = str(x).strip().upper()
        if u and u not in out:
            out.append(u)
    return out


def _frame(obj) -> pd.DataFrame:
    try:
        return pd.DataFrame(obj) if obj is not None else pd.DataFrame()
    except (TypeError, ValueError):
        return pd.DataFrame()


def _instantiate(cls, *, symbol: str | None = None, source: str | None = None):
    """Khởi tạo class với bộ tham số đầu tiên mà constructor chấp nhận."""
    candidates: list[dict] = []
    if symbol and source:
        candidates += [{"symbol": symbol, "source": source, "show_log": False}, {"symbol": symbol, "source": source}]
    if symbol:
        candidates += [{"symbol": symbol, "show_log": False}, {"symbol": symbol}]
    if source:
        candidates += [{"source": source, "show_log": False}, {"source": source}]
    candidates += [{"show_log": False}, {}]

    try:
        params = inspect.signature(cls).parameters
        accepts_any = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
    except (TypeError, ValueError):
        params, accepts_any = {}, True

    last: Exception | None = None
    for kwargs in candidates:
        if not accepts_any:
            kwargs = {k: v for k, v in kwargs.items() if k in params}
        try:
            return cls(**kwargs)
        except Exception as exc:  # thư viện ngoài có thể ném bất kỳ lỗi nào
            last = exc
    raise ProviderUnavailable(f"Không khởi tạo được {getattr(cls, '__name__', cls)}: {last}")


def _find_class(class_name: str, module_names: Iterable[str]):
    """Trả về (class, 'module.Class') ở module đầu tiên có class đó."""
    for module_name in module_names:
        try:
            cls = getattr(importlib.import_module(module_name), class_name, None)
        except Exception:  # module không tồn tại ở bản vnstock đang cài
            continue
        if cls is not None:
            return cls, f"{module_name}.{class_name}"
    raise ProviderUnavailable(f"Không tìm thấy class {class_name}")


def _provider(class_name: str, source: str, symbol: str):
    """Tạo đối tượng Quote/Trading đúng nguồn. Trả về (object, backend)."""
    # 1. Ưu tiên namespace API rõ ràng của vnstock 4.x để không chạm lớp Vnstock() cũ.
    if class_name == "Quote":
        try:
            from vnstock.api.quote import Quote
            try:
                return Quote(symbol=symbol, source=source), f"vnstock.api.quote.{source}"
            except Exception:
                return Quote(symbol=symbol), "vnstock.api.quote.default"
        except Exception:
            pass

    # 2. Cơ chế fallback cũ nếu dùng phiên bản khác
    if source == "MAS":
        try:
            cls, backend = _find_class(class_name, ["vnstock_data"])
        except ProviderUnavailable as exc:
            raise ProviderUnavailable("MAS cần gói vnstock_data (Sponsor)") from exc
    elif source in {"VCI", "KBS", "TCBS", "VND"}:
        sub = source.lower()
        cls, backend = _find_class(class_name, [
            f"vnstock.api.{class_name.lower()}",
            "vnstock",
            f"vnstock.explorer.{sub}",
            f"vnstock.explorer.{sub}.{class_name.lower()}",
        ])
    else:
        raise ProviderUnavailable(f"Nguồn chưa hỗ trợ: {source}")

    return _instantiate(cls, symbol=symbol, source=source), backend


def _call_first_nonempty(fn, kwargs_list: list[dict], label: str) -> pd.DataFrame:
    """Thử lần lượt các kiểu truyền tham số, trả DataFrame khác rỗng đầu tiên."""
    last: Exception | None = None
    for kwargs in kwargs_list:
        try:
            df = _frame(fn(**kwargs))
            if not df.empty:
                return df
            last = ProviderUnavailable(f"{label} trả DataFrame rỗng")
        except Exception as exc:
            last = exc
    raise ProviderUnavailable(f"{label} lỗi: {type(last).__name__}: {last}")


def _try_sources(sources, fetch_one, label: str) -> ProviderResult:
    """Gọi fetch_one(source) theo thứ tự nguồn, gom lỗi nếu tất cả đều hỏng."""
    errors = []
    for src in _source_order(sources):
        try:
            return fetch_one(src)
        except Exception as exc:
            errors.append(f"{src}: {type(exc).__name__}: {str(exc)[:180]}")
    raise ProviderUnavailable(" | ".join(errors) or f"Không có nguồn {label} khả dụng")


# ---------------------------------------------------------------- lịch sử giá
def _history_once(symbol: str, source: str, start: str, end: str, interval: str = "1D") -> ProviderResult:
    quote, backend = _provider("Quote", source, symbol)
    fn = getattr(quote, "history", None)
    if fn is None:
        raise ProviderUnavailable(f"{backend} không có history()")
    df = _call_first_nonempty(fn, [
        {"start": start, "end": end, "interval": interval},
        {"symbol": symbol, "start": start, "end": end, "interval": interval},
    ], f"{source} history")
    return ProviderResult(df, source, backend)


def fetch_history(symbol: str, *, sources: Iterable[str] = DEFAULT_SOURCES, start: str | None = None,
                  end: str | None = None, calendar_days: int = 620, interval: str = "1D") -> ProviderResult:
    """OHLCV theo ngày của một mã (hoặc VNINDEX), thử nguồn theo thứ tự."""
    e = date.fromisoformat(end) if end else date.today()
    s = date.fromisoformat(start) if start else e - timedelta(days=max(370, int(calendar_days)))
    symbol = symbol.upper()
    return _try_sources(
        sources, lambda src: _history_once(symbol, src, s.isoformat(), e.isoformat(), interval), "history"
    )


# ---------------------------------------------------------------- bảng giá
def _price_board_once(symbols: list[str], source: str) -> ProviderResult:
    trading, backend = _provider("Trading", source, symbols[0])
    fn = getattr(trading, "price_board", None)
    if fn is None:
        raise ProviderUnavailable(f"{backend} không có price_board()")
    df = _call_first_nonempty(fn, [{"symbols_list": symbols}, {"symbols": symbols}], f"{source} price_board")
    return ProviderResult(df, source, backend)


def fetch_single_quote(symbol: str, *, sources: Iterable[str] = DEFAULT_SOURCES) -> ProviderResult:
    """Bảng giá hiện tại của một mã. Nguồn cuối cùng là API hợp nhất vnstock.Market."""
    symbols = [symbol.upper()]
    try:
        return _try_sources(sources, lambda src: _price_board_once(symbols, src), "price board")
    except ProviderUnavailable as first_error:
        try:
            from vnstock import Market
            df = _frame(Market().quote(symbols))
            if not df.empty:
                return ProviderResult(df, "AUTO/KBS", "vnstock.Market.quote")
        except Exception as exc:
            raise ProviderUnavailable(f"{first_error} | AUTO: {type(exc).__name__}: {exc}") from exc
        raise


# ---------------------------------------------------------------- danh sách mã
def fetch_listing() -> ProviderResult:
    """Danh sách mã niêm yết: ưu tiên VCI, dự phòng vnstock.Reference."""
    errors = []
    try:
        cls, backend = _find_class("Listing", ["vnstock.explorer.vci", "vnstock.explorer.vci.listing"])
        listing = _instantiate(cls)
        for method in ("symbols_by_exchange", "all_symbols"):
            fn = getattr(listing, method, None)
            df = _frame(fn()) if fn else pd.DataFrame()
            if not df.empty:
                return ProviderResult(df, "VCI", f"{backend}.{method}")
    except Exception as exc:
        errors.append(f"VCI: {type(exc).__name__}: {str(exc)[:160]}")
    try:
        from vnstock import Reference
        df = _frame(Reference().equity.list_by_exchange())
        if not df.empty:
            return ProviderResult(df, "AUTO/KBS", "vnstock.Reference.equity.list_by_exchange")
    except Exception as exc:
        errors.append(f"AUTO: {type(exc).__name__}: {str(exc)[:160]}")
    raise ProviderUnavailable(" | ".join(errors) or "Không có nguồn danh sách mã")
