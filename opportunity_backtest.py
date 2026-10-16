"""Backtest long-only theo đặc tả "Kế hoạch kiểm thử Backtest cho StockInsight".

Mục đích là kiểm định học thuật bộ quy tắc 60/40, không phải mô phỏng môi giới đầy đủ.
Nguyên tắc:
- Tín hiệu tính sau khi phiên t đóng cửa, khớp tại giá mở cửa phiên t+1.
- Một vị thế mỗi mã (FLAT/LONG); không mua chồng, không bán khống, không vay.
- Mọi tín hiệu đều có trạng thái xử lý (EXECUTED, IGNORED_ALREADY_LONG, ...).
- Cuối kỳ còn cổ phiếu thì định giá theo giá đóng cửa, KHÔNG tự bán để làm đẹp
  số giao dịch hoàn tất.
- So sánh với Buy & Hold và VN-Index trên cùng trục thời gian.
- Fundamental statements are truncated to the latest quarter assumed published
  by day t (quarter-end + publication lag) to reduce look-ahead bias.
- Historical P/E and P/B are deliberately omitted when reliable point-in-time
  valuation data are unavailable; the fundamental block is normalized over the
  other available criteria and the lower coverage is reported.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd

import chart_service as charts
import investment_analysis as ia
import stock_service as service
from utils import to_float


SUPPORTED_BACKTEST_MONTHS = (12,)          # nhóm thống nhất: chỉ backtest 1 năm
SUPPORTED_BACKTEST_YEARS = (1,)   # tương thích tham số ?years= cũ
PERIOD_LABELS = {12: "1 năm"}
PERIOD_ERROR = "Chỉ hỗ trợ backtest 1 năm."
WARMUP_SESSIONS = 60          # đủ cho MA50 và RSI14 trước ngày bắt đầu
WARMUP_CALENDAR_DAYS = 200    # khoảng lịch tải thêm để có WARMUP_SESSIONS

STATUS_EXECUTED = "EXECUTED"
STATUS_IGNORED_ALREADY_LONG = "IGNORED_ALREADY_LONG"
STATUS_IGNORED_NO_POSITION = "IGNORED_NO_POSITION"
STATUS_REJECTED_CASH = "REJECTED_INSUFFICIENT_CASH"
STATUS_NO_PRICE = "NOT_EXECUTED_NO_PRICE"
STATUS_END_OF_PERIOD = "NOT_EXECUTED_END_OF_PERIOD"
STATUS_NO_ORDER = "NO_ORDER"                         # HOLD: không phát sinh lệnh
STATUS_NO_ORDER_NODATA = "NO_ORDER_INSUFFICIENT_DATA"  # chưa đủ dữ liệu: không tự gán lệnh

STATUS_REASONS = {
    STATUS_IGNORED_ALREADY_LONG: "Đã có vị thế, không mua thêm theo quy tắc quản lý vốn.",
    STATUS_IGNORED_NO_POSITION: "Không có vị thế, không bán khống.",
    STATUS_REJECTED_CASH: "Không đủ tiền mua tối thiểu một lô kèm phí.",
    STATUS_NO_PRICE: "Không có giá mở cửa hợp lệ ở phiên thực thi.",
    STATUS_END_OF_PERIOD: "Không còn phiên kế tiếp trong kỳ kiểm thử.",
    STATUS_NO_ORDER: "Tín hiệu HOLD, không phát sinh lệnh mới.",
    STATUS_NO_ORDER_NODATA: "Chưa đủ dữ liệu, không tự động phát lệnh.",
}


@dataclass
class BacktestSettings:
    months: int = 12
    rebalance_every: int = 1          # đánh giá tín hiệu sau MỖI phiên
    publication_lag_days: int = 45
    initial_capital: float = 100_000_000.0
    buy_fee_pct: float = 0.001
    sell_fee_pct: float = 0.001
    sell_tax_pct: float = 0.001
    slippage_pct: float = 0.001
    lot_size: int = 100

    @property
    def years(self) -> float:
        return self.months / 12

    def as_dict(self) -> dict[str, Any]:
        return {
            "months": self.months,
            "period_label": PERIOD_LABELS.get(self.months, f"{self.months} tháng"),
            "initial_capital": self.initial_capital,
            "buy_fee_pct": self.buy_fee_pct * 100,
            "sell_fee_pct": self.sell_fee_pct * 100,
            "sell_tax_pct": self.sell_tax_pct * 100,
            "slippage_pct": self.slippage_pct * 100,
            "lot_size": self.lot_size,
            "signal_every_sessions": self.rebalance_every,
            "publication_lag_days": self.publication_lag_days,
            "execution_rule": "Tín hiệu sau phiên t, khớp giá mở cửa phiên t+1 ± trượt giá",
            "position_rule": "Một vị thế/mã, không mua chồng, không bán khống, không đòn bẩy",
            "benchmarks": ["Buy & Hold", "VN-Index"],
        }


def _quarter_end(year: int, quarter: int) -> date:
    return (pd.Timestamp(year=year, month=quarter * 3, day=1) + pd.offsets.MonthEnd(0)).date()


def _available_quarter(day: date, lag_days: int) -> tuple[int, int]:
    y, q = day.year, (day.month - 1) // 3 + 1
    while _quarter_end(y, q) + timedelta(days=lag_days) > day:
        y, q = (y, q - 1) if q > 1 else (y - 1, 4)
    return y, q


def _fundamental_asof(symbol: str, tables: dict, day: date, lag_days: int) -> dict[str, Any]:
    kind = ia.company_type(symbol)
    upto = _available_quarter(day, lag_days)
    income = service.truncate_quarters(tables.get("income_q", pd.DataFrame()), upto)
    balance = service.truncate_quarters(tables.get("balance_q", pd.DataFrame()), upto)
    revenue_phrases = ia.REVENUE_FINANCIAL if kind != "regular" else ia.REVENUE_REGULAR

    revenue_growth = ia._growth_yoy(income, revenue_phrases)
    profit_growth = ia._growth_yoy(income, service._NPAT_PARENT_PHRASES)
    if profit_growth is None:
        profit_growth = ia._growth_yoy(income, service._NPAT_TOTAL_PHRASES)
    roe = service._compute_roe_ttm(income, balance)
    roa = ia._compute_roa_ttm(income, balance)
    de = None if kind != "regular" else service._compute_debt_equity(balance)
    w = ia._weights(kind)

    criteria = {
        "revenue_growth": ia._criterion("Tăng trưởng doanh thu/thu nhập", revenue_growth, w["revenue_growth"],
                                        ia._growth_points(revenue_growth, w["revenue_growth"]), "%", group="growth"),
        "profit_growth": ia._criterion("Tăng trưởng LNST", profit_growth, w["profit_growth"],
                                       ia._growth_points(profit_growth, w["profit_growth"], profit=True), "%", group="growth"),
        "roe": ia._criterion("ROE TTM", roe, w["roe"], ia._roe_points(roe, w["roe"]), "%", group="profitability_leverage"),
        "roa": ia._criterion("ROA TTM", roa, w["roa"], ia._roa_points(roa, w["roa"], kind), "%", group="profitability_leverage"),
        # Point-in-time valuation is not reconstructed from current ratios.
        "pe": ia._criterion("P/E", None, w["pe"], None, "x", "Không dùng trong backtest nếu thiếu dữ liệu định giá lịch sử.", "valuation"),
        "pb": ia._criterion("P/B", None, w["pb"], None, "x", "Không dùng trong backtest nếu thiếu dữ liệu định giá lịch sử.", "valuation"),
    }
    if kind == "regular":
        criteria["de"] = ia._criterion("D/E", de, w["de"], ia._de_points(de, w["de"]), "x", group="profitability_leverage")
    return {
        "company_type": kind,
        "criteria": criteria,
        "latest_period": service._latest_period(income, balance),
    }


def _max_drawdown(values: list[float]) -> float:
    if not values:
        return 0.0
    arr = np.asarray(values, dtype=float)
    peak = np.maximum.accumulate(arr)
    dd = arr / peak - 1.0
    return float(dd.min() * 100)


def _buy_position(cash: float, market_price: float, settings: BacktestSettings) -> dict[str, float | int]:
    execution_price = market_price * (1 + settings.slippage_pct)
    unit_cost = execution_price * (1 + settings.buy_fee_pct)
    quantity = int(cash // (unit_cost * settings.lot_size)) * settings.lot_size
    gross_value = quantity * execution_price
    commission = gross_value * settings.buy_fee_pct
    return {
        "quantity": quantity,
        "execution_price": execution_price,
        "gross_value": gross_value,
        "commission": commission,
        "slippage_cost": quantity * (execution_price - market_price),
        "cash_after": cash - gross_value - commission,
    }


def _sell_position(quantity: int, market_price: float, settings: BacktestSettings) -> dict[str, float]:
    execution_price = market_price * (1 - settings.slippage_pct)
    gross_value = quantity * execution_price
    commission = gross_value * settings.sell_fee_pct
    sell_tax = gross_value * settings.sell_tax_pct
    return {
        "execution_price": execution_price,
        "gross_value": gross_value,
        "commission": commission,
        "sell_tax": sell_tax,
        "slippage_cost": quantity * (market_price - execution_price),
        "net_proceeds": gross_value - commission - sell_tax,
    }


def _settings_for_period(*, months: Any = None, years: Any = None) -> BacktestSettings:
    """Kiểm tra kỳ backtest: chỉ 1 năm (months=12 hoặc years=1)."""
    if months is not None:
        value = months
        allowed = SUPPORTED_BACKTEST_MONTHS
        factor = 1
    else:
        value = years
        allowed = SUPPORTED_BACKTEST_YEARS
        factor = 12
    if isinstance(value, bool) or value is None:
        raise ValueError(PERIOD_ERROR)
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(PERIOD_ERROR) from exc
    if not number.is_integer() or int(number) not in allowed:
        raise ValueError(PERIOD_ERROR)
    return BacktestSettings(months=int(number) * factor)


def _settings_for_years(years: Any) -> BacktestSettings:
    return _settings_for_period(years=years)


def _max_drawdown(values: list[float]) -> float:
    if not values:
        return 0.0
    arr = np.asarray(values, dtype=float)
    peak = np.maximum.accumulate(arr)
    dd = arr / peak - 1.0
    return float(dd.min() * 100)


def _annualized(final_value: float | None, initial: float, days: int) -> float | None:
    if final_value is None or days <= 0 or initial <= 0 or final_value <= 0:
        return None
    return ((final_value / initial) ** (365.25 / days) - 1) * 100


def _round(x: Any, digits: int = 2) -> Any:
    v = to_float(x)
    return None if v is None else round(v, digits)


def _clean_prices(hist: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Chuẩn hóa ngày, sắp xếp, loại trùng và đếm dòng giá thiếu/không hợp lệ."""
    x = hist.copy()
    x["time"] = pd.to_datetime(x["time"], errors="coerce")
    for col in ("open", "high", "low", "close", "volume"):
        if col not in x.columns:
            x[col] = np.nan
        x[col] = pd.to_numeric(x[col], errors="coerce")
    x = x.dropna(subset=["time"]).sort_values("time")
    duplicates = int(x["time"].duplicated(keep="last").sum())
    x = x.drop_duplicates(subset=["time"], keep="last")

    missing = x[["open", "high", "low", "close"]].isna().any(axis=1)
    bad_close = x["close"].isna() | (x["close"] <= 0)
    ohlc_ok = (
        (x["high"] >= x[["open", "close", "low"]].max(axis=1))
        & (x["low"] <= x[["open", "close", "high"]].min(axis=1))
        & (x[["open", "high", "low", "close"]] > 0).all(axis=1)
    )
    invalid = ~missing & ~ohlc_ok
    quality = {
        "duplicate_dates": duplicates,
        "missing_price_rows": int(missing.sum()),
        "invalid_price_rows": int(invalid.sum()),
        "dropped_rows_no_close": int(bad_close.sum()),
    }
    # Không có giá đóng cửa hợp lệ thì không định giá được tài sản: loại khỏi trục thời gian.
    x = x[~bad_close].reset_index(drop=True)
    return x, quality


def _load_prices(symbol: str, settings: BacktestSettings) -> tuple[pd.DataFrame, bool, list[str]]:
    """Giá lịch sử đủ cho kỳ backtest + giai đoạn khởi tạo chỉ báo.

    get_history giữ khoảng 620 ngày lịch; nếu nguồn trả thiếu phiên thì tải
    theo khoảng ngày bằng fetch_history_range.
    """
    warnings: list[str] = []
    hist, _, stale = service.get_history(symbol)
    hist = service.confirmed_bars(hist)
    need_from = pd.Timestamp.today().normalize() - pd.DateOffset(months=settings.months) - pd.Timedelta(days=WARMUP_CALENDAR_DAYS)
    first = pd.to_datetime(hist["time"]).min() if hist is not None and len(hist) else None
    if first is None or first > need_from:
        try:
            longer = service.fetch_history_range(symbol, need_from.date().isoformat(), date.today().isoformat())
            longer = service.confirmed_bars(longer)
            if longer is not None and len(longer) > (0 if hist is None else len(hist)):
                hist = longer
        except Exception as exc:  # nguồn ngoài lỗi: dùng dữ liệu đang có và cảnh báo
            warnings.append(f"Không tải thêm được lịch sử giá dài hạn: {type(exc).__name__}.")
    return hist, bool(stale), warnings


def _load_vnindex(start: pd.Timestamp, end: pd.Timestamp) -> tuple[pd.DataFrame | None, str | None]:
    try:
        idx = service.fetch_history_range(
            "VNINDEX", (start - pd.Timedelta(days=10)).date().isoformat(), end.date().isoformat(), index=True
        )
    except Exception as exc:
        return None, f"Không tải được VN-Index: {type(exc).__name__}."
    if idx is None or len(idx) == 0:
        return None, "Nguồn không trả dữ liệu VN-Index."
    idx = idx.copy()
    idx["time"] = pd.to_datetime(idx["time"]).dt.normalize()
    idx["close"] = pd.to_numeric(idx["close"], errors="coerce")
    idx = idx.dropna(subset=["close"]).drop_duplicates("time", keep="last").sort_values("time")
    return idx[["time", "close"]], None


def _series_stats(values: list[float], initial: float, days: int) -> dict[str, Any]:
    final = values[-1] if values else None
    return {
        "final_value": final,
        "net_pnl": None if final is None else final - initial,
        "return_pct": None if final is None else (final / initial - 1) * 100,
        "annualized_return_pct": _annualized(final, initial, days),
        "max_drawdown_pct": _max_drawdown(values) if values else None,
    }


def run_backtest(symbol: str, years: Any = None, *, months: Any = None, include_vnindex: bool = True) -> dict[str, Any]:
    if months is None and years is None:
        months = 12
    settings = _settings_for_period(months=months, years=years) if months is not None else _settings_for_period(years=years)
    s = symbol.upper().strip()
    run_at = pd.Timestamp.now().isoformat(timespec="seconds")

    raw_hist, stale, warnings = _load_prices(s, settings)
    hist, quality = _clean_prices(raw_hist)
    if len(hist) < 90:
        raise service.DataUnavailable("Cần ít nhất khoảng 90 phiên để chạy backtest.")

    n = len(hist)
    end = hist["time"].iloc[-1]
    start_cut = end - pd.DateOffset(months=settings.months)
    first_eval = int(hist["time"].searchsorted(start_cut))
    if first_eval < WARMUP_SESSIONS:
        raise service.DataUnavailable(
            f"Không đủ dữ liệu để kiểm định trọn {PERIOD_LABELS.get(settings.months)} "
            f"cùng {WARMUP_SESSIONS} phiên khởi tạo chỉ báo; nguồn chỉ có {n} phiên."
        )
    if first_eval >= n - 2:
        raise service.DataUnavailable("Không đủ lịch sử giá trong khoảng backtest đã chọn.")

    try:
        tables, _, _ = service.get_fundamental_tables(s)
    except Exception:
        tables = {"income_q": pd.DataFrame(), "balance_q": pd.DataFrame(), "source": "?"}
        warnings.append("Không tải được BCTC; điểm cơ bản có thể thiếu và tín hiệu là KHÔNG ĐỦ DỮ LIỆU.")

    # Chỉ báo kỹ thuật đều là phép tính nhân quả (chỉ dùng dữ liệu ≤ phiên t), nên tính
    # một lần trên toàn bộ chuỗi rồi cắt tiền tố [: t + 1] cho kết quả giống hệt tính lại.
    frame_all = charts.compute_indicators(hist)
    per_day_frames = len(frame_all) != n
    fund_cache: dict[tuple[int, int], dict] = {}

    def fundamentals_at(day: date) -> dict:
        key = _available_quarter(day, settings.publication_lag_days)
        if key not in fund_cache:
            fund_cache[key] = _fundamental_asof(s, tables, day, settings.publication_lag_days)
        return fund_cache[key]

    cash = settings.initial_capital
    shares = 0
    position: dict[str, Any] | None = None
    pending: dict[str, Any] | None = None   # lệnh chờ khớp ở phiên kế tiếp
    orders: list[dict[str, Any]] = []        # nhật ký lệnh đã thực thi
    signal_log: list[dict[str, Any]] = []    # mọi tín hiệu + trạng thái xử lý
    round_trips: list[dict[str, Any]] = []   # chu kỳ mua-bán đã đóng
    equity_rows: list[dict[str, Any]] = []
    signal_counts = {"BUY": 0, "HOLD": 0, "SELL": 0, "NODATA": 0}
    total_commission = total_sell_tax = total_slippage = 0.0

    def set_status(entry: dict, status: str, reason: str | None = None) -> None:
        entry["status"] = status
        entry["reason"] = reason or STATUS_REASONS.get(status, "")

    for i in range(first_eval, n):
        row = hist.iloc[i]
        day = pd.Timestamp(row["time"]).date()
        close = float(row["close"])

        # 1) Khớp lệnh chờ từ tín hiệu phiên trước tại giá mở cửa hôm nay.
        if pending is not None:
            entry = pending["log"]
            entry["execution_date"] = day.isoformat()
            open_price = to_float(row.get("open"))
            if not open_price or open_price <= 0 or not np.isfinite(open_price):
                set_status(entry, STATUS_NO_PRICE)
            elif pending["side"] == "BUY":
                purchase = _buy_position(cash, open_price, settings)
                qty = int(purchase["quantity"])
                if qty <= 0:
                    set_status(entry, STATUS_REJECTED_CASH)
                else:
                    cash = float(purchase["cash_after"])
                    shares = qty
                    total_commission += float(purchase["commission"])
                    total_slippage += float(purchase["slippage_cost"])
                    position = {
                        "entry_date": day.isoformat(),
                        "entry_price": float(purchase["execution_price"]),
                        "quantity": qty,
                        "entry_cost": float(purchase["gross_value"]) + float(purchase["commission"]),
                        "buy_commission": float(purchase["commission"]),
                        "entry_slippage_cost": float(purchase["slippage_cost"]),
                    }
                    orders.append({
                        "trade_id": len(orders) + 1, "ticker": s,
                        "signal_date": entry["signal_date"], "signal": "BUY",
                        "execution_date": day.isoformat(), "side": "BUY",
                        "execution_price": _round(purchase["execution_price"]),
                        "quantity": qty,
                        "gross_value": _round(purchase["gross_value"]),
                        "commission": _round(purchase["commission"]),
                        "sell_tax": 0.0,
                        "net_cash_flow": _round(-(purchase["gross_value"] + purchase["commission"])),
                        "cash_after": _round(cash), "shares_after": shares,
                        "position_value_after": _round(shares * float(purchase["execution_price"])),
                        "status": STATUS_EXECUTED, "reason": "",
                    })
                    set_status(entry, STATUS_EXECUTED, "")
            else:  # SELL toàn bộ vị thế
                sale = _sell_position(shares, open_price, settings)
                proceeds = float(sale["net_proceeds"])
                total_commission += float(sale["commission"])
                total_sell_tax += float(sale["sell_tax"])
                total_slippage += float(sale["slippage_cost"])
                cash += proceeds
                orders.append({
                    "trade_id": len(orders) + 1, "ticker": s,
                    "signal_date": entry["signal_date"], "signal": "SELL",
                    "execution_date": day.isoformat(), "side": "SELL",
                    "execution_price": _round(sale["execution_price"]),
                    "quantity": shares,
                    "gross_value": _round(sale["gross_value"]),
                    "commission": _round(sale["commission"]),
                    "sell_tax": _round(sale["sell_tax"]),
                    "net_cash_flow": _round(proceeds),
                    "cash_after": _round(cash), "shares_after": 0,
                    "position_value_after": 0.0,
                    "status": STATUS_EXECUTED, "reason": "",
                })
                round_trips.append({
                    "entry_date": position["entry_date"],
                    "entry_price": _round(position["entry_price"]),
                    "exit_date": day.isoformat(),
                    "exit_price": _round(sale["execution_price"]),
                    "quantity": shares,
                    "return_pct": _round((proceeds / position["entry_cost"] - 1) * 100),
                    "net_pnl": _round(proceeds - position["entry_cost"]),
                    "buy_commission": _round(position["buy_commission"]),
                    "sell_commission": _round(sale["commission"]),
                    "sell_tax": _round(sale["sell_tax"]),
                    "total_costs": _round(
                        position["buy_commission"] + float(sale["commission"]) + float(sale["sell_tax"])
                        + position["entry_slippage_cost"] + float(sale["slippage_cost"])
                    ),
                })
                shares = 0
                position = None
                set_status(entry, STATUS_EXECUTED, "")
            pending = None

        # 2) Sau khi phiên đóng cửa: tính tín hiệu (chỉ dùng dữ liệu ≤ phiên i).
        code = None
        if (i - first_eval) % settings.rebalance_every == 0:
            frame = charts.compute_indicators(hist.iloc[: i + 1]) if per_day_frames else frame_all.iloc[: i + 1]
            tech = ia._technical(frame)
            fund = fundamentals_at(day)
            score = ia._score_all(fund, tech)
            signal = ia._signal_from_score(score, fund, tech)
            code = str(signal.get("code") or "NODATA").upper()
            if code not in signal_counts:
                code = "NODATA"
            signal_counts[code] += 1
            entry = {
                "ticker": s, "signal_date": day.isoformat(), "signal": code,
                "score": _round(score.get("score"), 1),
                "fundamental_score": _round((score.get("fundamental") or {}).get("score"), 1),
                "technical_score": _round((score.get("technical") or {}).get("score"), 1),
                "coverage": _round(score.get("coverage_pct"), 1),
                "execution_date": None, "status": "", "reason": "",
            }
            if code == "BUY":
                if shares > 0:
                    set_status(entry, STATUS_IGNORED_ALREADY_LONG)
                elif i == n - 1:
                    set_status(entry, STATUS_END_OF_PERIOD)
                else:
                    pending = {"side": "BUY", "log": entry}
            elif code == "SELL":
                if shares <= 0:
                    set_status(entry, STATUS_IGNORED_NO_POSITION)
                elif i == n - 1:
                    set_status(entry, STATUS_END_OF_PERIOD)
                else:
                    pending = {"side": "SELL", "log": entry}
            elif code == "HOLD":
                set_status(entry, STATUS_NO_ORDER)
            else:
                set_status(entry, STATUS_NO_ORDER_NODATA)
            signal_log.append(entry)

        # 3) Giá trị tài sản cuối phiên theo giá đóng cửa.
        position_value = shares * close
        equity_rows.append({
            "date": day.isoformat(), "close": close, "signal": code,
            "cash": cash, "shares": shares, "position_value": position_value,
            "equity": cash + position_value,
        })

    # Buy & Hold: mua ở giá mở cửa phiên đầu tiên có thể khớp, nắm giữ tới cuối kỳ.
    initial = settings.initial_capital
    bh_values: list[float] = []
    bh_info: dict[str, Any] = {"quantity": 0, "commission": 0.0}
    bh_open = to_float(hist.iloc[first_eval + 1].get("open"))
    if bh_open and bh_open > 0:
        bh = _buy_position(initial, bh_open, settings)
        bh_info = {"quantity": int(bh["quantity"]), "commission": float(bh["commission"]),
                   "entry_price": float(bh["execution_price"]), "cash": float(bh["cash_after"])}
    else:
        warnings.append("Không có giá mở cửa hợp lệ để mua Buy & Hold ở phiên đầu kỳ.")
    for k, eq_row in enumerate(equity_rows):
        if k == 0 or not bh_info["quantity"]:
            bh_values.append(initial)
        else:
            bh_values.append(bh_info["cash"] + bh_info["quantity"] * eq_row["close"])

    # VN-Index: quy đổi biến động chỉ số thành giá trị từ cùng vốn ban đầu.
    vn_values: list[float | None] = [None] * len(equity_rows)
    vn_note = None
    if include_vnindex:
        start_ts = pd.Timestamp(equity_rows[0]["date"])
        idx, vn_note = _load_vnindex(start_ts, pd.Timestamp(equity_rows[-1]["date"]))
        if idx is not None:
            base_rows = idx[idx["time"] <= start_ts]
            if base_rows.empty:
                vn_note = "VN-Index thiếu dữ liệu tại ngày bắt đầu kỳ."
            else:
                base = float(base_rows["close"].iloc[-1])
                closes = dict(zip(idx["time"], idx["close"]))
                missing_days, last = 0, base
                for k, eq_row in enumerate(equity_rows):
                    v = closes.get(pd.Timestamp(eq_row["date"]))
                    if v is None:
                        missing_days += 1   # giữ mức chỉ số phiên trước, ghi nhận giới hạn
                    else:
                        last = float(v)
                    vn_values[k] = initial * last / base
                if missing_days:
                    vn_note = f"VN-Index thiếu {missing_days} phiên so với trục thời gian của mã; giữ mức phiên trước."
    else:
        vn_note = "Không yêu cầu so sánh VN-Index."
    if vn_note:
        warnings.append(vn_note)

    # Chuỗi tài sản: lợi suất lũy kế, đỉnh và drawdown.
    peak = -np.inf
    for k, eq_row in enumerate(equity_rows):
        peak = max(peak, eq_row["equity"])
        eq_row["cumulative_return"] = round((eq_row["equity"] / initial - 1) * 100, 4)
        eq_row["peak_equity"] = round(peak, 2)
        eq_row["drawdown"] = round((eq_row["equity"] / peak - 1) * 100, 4)
        eq_row["buy_hold_equity"] = round(bh_values[k], 2)
        eq_row["vnindex_equity"] = None if vn_values[k] is None else round(vn_values[k], 2)
        for key in ("cash", "position_value", "equity"):
            eq_row[key] = round(eq_row[key], 2)

    start_date = equity_rows[0]["date"]
    end_date = equity_rows[-1]["date"]
    days = (pd.Timestamp(end_date) - pd.Timestamp(start_date)).days
    strat_stats = _series_stats([r["equity"] for r in equity_rows], initial, days)
    bh_stats = _series_stats(bh_values, initial, days) if bh_info["quantity"] else _series_stats([], initial, days)
    vn_series = [v for v in vn_values if v is not None]
    vn_stats = _series_stats(vn_series, initial, days) if len(vn_series) == len(vn_values) else _series_stats([], initial, days)

    wins = [t for t in round_trips if (to_float(t.get("return_pct")) or 0) > 0]
    win_rate = round(len(wins) / len(round_trips) * 100, 2) if round_trips else None
    open_position = None
    if shares > 0 and position is not None:
        final_close = equity_rows[-1]["close"]
        open_position = {
            "entry_date": position["entry_date"],
            "entry_price": _round(position["entry_price"]),
            "quantity": shares,
            "last_close": final_close,
            "market_value": _round(shares * final_close),
            "unrealized_pnl": _round(shares * final_close - position["entry_cost"]),
            "unrealized_return_pct": _round((shares * final_close / position["entry_cost"] - 1) * 100),
            "note": "Vị thế chưa đóng, định giá theo giá đóng cửa cuối kỳ; không tự bán.",
        }

    unexecuted = sum(1 for e in signal_log if e["status"] in (STATUS_REJECTED_CASH, STATUS_NO_PRICE, STATUS_END_OF_PERIOD))
    status_counts: dict[str, int] = {}
    for e in signal_log:
        status_counts[e["status"]] = status_counts.get(e["status"], 0) + 1

    def metric(label, strat, bh, vn, unit):
        return {"metric": label, "unit": unit, "stockinsight": strat, "buy_hold": bh, "vnindex": vn}

    summary = [
        metric("Vốn ban đầu", initial, initial, initial, "VND"),
        metric("Giá trị tài sản cuối kỳ", _round(strat_stats["final_value"]), _round(bh_stats["final_value"]), _round(vn_stats["final_value"]), "VND"),
        metric("Lãi/lỗ ròng", _round(strat_stats["net_pnl"]), _round(bh_stats["net_pnl"]), _round(vn_stats["net_pnl"]), "VND"),
        metric("Tỷ suất sinh lời", _round(strat_stats["return_pct"]), _round(bh_stats["return_pct"]), _round(vn_stats["return_pct"]), "%"),
        metric("Tỷ suất sinh lời năm hóa", _round(strat_stats["annualized_return_pct"]), _round(bh_stats["annualized_return_pct"]), _round(vn_stats["annualized_return_pct"]), "%"),
        metric("Số giao dịch hoàn tất", len(round_trips), 0, None, "lần"),
        metric("Tỷ lệ giao dịch thắng", win_rate, None, None, "%"),
        metric("Max Drawdown", _round(strat_stats["max_drawdown_pct"]), _round(bh_stats["max_drawdown_pct"]), _round(vn_stats["max_drawdown_pct"]), "%"),
        metric("Tổng phí giao dịch", _round(total_commission), _round(bh_info["commission"]) if bh_info["quantity"] else None, None, "VND"),
        metric("Thuế bán", _round(total_sell_tax), 0.0 if bh_info["quantity"] else None, None, "VND"),
    ]

    data_quality = {
        "status": "SUCCESS",
        "ticker": s,
        "start_date": start_date,
        "end_date": end_date,
        "trading_sessions": len(equity_rows),
        "missing_price_rows": quality["missing_price_rows"],
        "invalid_price_rows": quality["invalid_price_rows"],
        "duplicate_dates": quality["duplicate_dates"],
        "invalid_signals": 0,
        "unexecuted_orders": unexecuted,
        "warnings": warnings,
        "error_message": None,
    }
    if open_position:
        warnings.append("Cuối kỳ còn vị thế mở; giá trị được tính theo giá đóng cửa, chưa trừ phí/thuế bán.")

    period_label = PERIOD_LABELS.get(settings.months, f"{settings.months} tháng")
    return {
        "status": "SUCCESS",
        "symbol": s,
        "months": settings.months,
        "years": settings.years,
        "period_label": period_label,
        "start_date": start_date,
        "end_date": end_date,
        "trading_sessions": len(equity_rows),
        "config": {**settings.as_dict(), "ticker": s, "run_at": run_at,
                   "start_date": start_date, "end_date": end_date},
        "summary": summary,
        "trades": len(round_trips),                 # số giao dịch hoàn tất (đã đóng)
        "orders": orders,                           # nhật ký lệnh đầy đủ
        "signal_log": signal_log,                   # mọi tín hiệu + trạng thái
        "status_counts": status_counts,
        "trade_log": round_trips,                   # chu kỳ mua-bán đã đóng
        "open_position": open_position,
        "equity_curve": equity_rows,
        "data_quality": data_quality,
        # Các khóa cũ, giữ cho giao diện/PDF hiện có.
        "strategy_return_pct": _round(strat_stats["return_pct"]),
        "cagr_pct": _round(strat_stats["annualized_return_pct"]),
        "buy_hold_return_pct": _round(bh_stats["return_pct"]),
        "vnindex_return_pct": _round(vn_stats["return_pct"]),
        "max_drawdown_pct": _round(strat_stats["max_drawdown_pct"]),
        "win_rate_pct": win_rate,
        "transaction_costs_vnd": _round(total_commission + total_sell_tax),
        "slippage_costs_vnd": _round(total_slippage),
        "total_costs_vnd": _round(total_commission + total_sell_tax + total_slippage),
        "skipped_orders": unexecuted,
        "signal_counts": signal_counts,
        "observations": signal_log[-60:],
        "data_stale": bool(stale),
        "method_note": (
            f"Kỳ {period_label} ({start_date} → {end_date}, {len(equity_rows)} phiên). Vốn 100 triệu VNĐ; "
            "tín hiệu tính sau mỗi phiên, khớp giá mở cửa phiên kế tiếp; lô 100 cổ phiếu, phí mua/bán 0,10%, "
            "thuế bán 0,10%, trượt giá 0,10% (đã nằm trong giá khớp, không trừ lần hai). BCTC chỉ dùng tới kỳ "
            "giả định đã công bố (trễ 45 ngày); không dùng P/E, P/B hiện tại hồi tố. Cuối kỳ còn cổ phiếu thì "
            "định giá theo giá đóng cửa, không tự bán."
        ),
        "limitation": (
            "Chưa tính cổ tức, giới hạn thanh khoản, biên độ trần/sàn và các sự kiện doanh nghiệp; "
            "VN-Index là chuẩn tham chiếu theo biến động chỉ số, không phải danh mục mua được. "
            "Kết quả quá khứ không đảm bảo hiệu quả tương lai."
        ),
    }
