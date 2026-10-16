"""Backtest chiến lược của FinTech Bot trên dữ liệu quá khứ.

Chạy: python backtest.py                      (15 mã mặc định, 6 tháng gần nhất)
      python backtest.py --period 3M
      python backtest.py --symbols FPT,HPG --start 2026-01-01 --end 2026-06-30

Chuỗi xử lý (theo kế hoạch Backtest):
    Data Layer -> Indicator Layer -> Strategy Layer -> Backtest Engine -> Portfolio
    -> Trade Log -> Metrics -> Report/Chart

Nguyên tắc chống sai lệch:
- Không Look-ahead: tín hiệu tính trên nến đóng cửa ngày t (chỉ dùng dữ liệu <= t),
  lệnh khớp ở giá MỞ CỬA phiên t+1.
- Không Data Leakage BCTC: quý Q chỉ được dùng sau ngày kết thúc quý + BACKTEST_FUND_LAG_DAYS.
- Dùng NGUYÊN bộ quy tắc strategy.evaluate() của bot (không viết chiến lược riêng):
  cùng chỉ báo, cùng cổng thị trường, cùng bộ lọc cứng, cùng điều kiện thoát lệnh.
- Mỗi ngày engine đưa cho chiến lược đúng cửa sổ lịch sử mà bot live nhìn thấy
  (620 ngày lịch, giống stock_service._load_history).
- Mua: chỉ khớp khi Open(t+1) > Pivot và <= vùng mua (đúng hướng dẫn trong tín hiệu BUY);
  Open(t+1) ở giá trần -> đánh dấu "không mô phỏng được khớp lệnh" và BỎ QUA.
- Bán: cổ phiếu phải về tài khoản (T+2) mới bán được; phiên trắng bên mua ở giá sàn -> dời sang phiên sau.
- Phí giao dịch 2 chiều + thuế bán 0,1% được trừ vào lãi/lỗ.

Module thuần tính toán, trừ phần Data Layer (gọi VNStock qua stock_service, có cache đĩa
trong data/backtest_cache để chạy lại nhanh và không tốn quota).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import logging
import math
import pickle
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

import strategy
from config import BACKTEST, FINANCIAL_SYMBOLS, STRATEGY, BacktestConfig
from utils import fmt_money, fmt_price, to_float

log = logging.getLogger("fintech-bot.backtest")

BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "data" / "backtest_cache"
REPORT_DIR = BASE_DIR / "data" / "backtest_reports"

PERIOD_MONTHS = {"1M": 1, "3M": 3, "6M": 6, "1Y": 12}
PRICE_LIMIT = {"HOSE": 0.07, "HNX": 0.10}


class BacktestBusy(RuntimeError):
    """Đang có một backtest khác chạy (dùng chung quota VNStock nên chỉ chạy một lượt)."""


# =====================================================================
# 0. Tham số khoảng thời gian
# =====================================================================
def _parse_date(value) -> date | None:
    """'2026-03-31' | '31/03/2026' | date -> date; rỗng -> None."""
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    raw = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Ngày không hợp lệ: {value!r} (dùng YYYY-MM-DD hoặc dd/mm/yyyy)")


def resolve_period(start=None, end=None, months: int | None = None, cfg: BacktestConfig = BACKTEST,
                   today: date | None = None) -> tuple[date, date]:
    """Xác định [BACKTEST_START, BACKTEST_END].

    Ưu tiên tham số truyền vào, sau đó .env; END mặc định là hôm nay, START mặc định
    lùi `months` (hoặc BACKTEST_MONTHS) tháng tính từ END.
    """
    today = today or date.today()
    e = _parse_date(end) or _parse_date(cfg.end) or today
    e = min(e, today)
    s = _parse_date(start)
    if s is None and months is None:
        s = _parse_date(cfg.start)
    if s is None:
        s = (pd.Timestamp(e) - pd.DateOffset(months=int(months or cfg.default_months))).date()
    if s >= e:
        raise ValueError("BACKTEST_START phải trước BACKTEST_END.")
    return s, e


# =====================================================================
# 1. Data Layer
# =====================================================================
@dataclass
class BacktestData:
    prices: dict[str, pd.DataFrame]          # OHLCV đã chuẩn hóa, gồm cả phần warm-up trước START
    index: pd.DataFrame                      # VN-Index OHLCV
    fund_tables: dict[str, dict]             # bảng BCTC theo quý (mọi kỳ), cắt theo thời điểm khi dùng
    meta: dict[str, dict]                    # {mã: {exchange, name}}
    warnings: list[str] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)


def _cache_path(key: str) -> Path:
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    safe = "".join(ch if ch.isalnum() else "_" for ch in key)[:60]
    return CACHE_DIR / f"{safe}_{digest}.pkl"


def _disk_cached(key: str, max_age_seconds: float, loader):
    """Cache pickle trên đĩa. Lỗi đọc/ghi cache không bao giờ làm hỏng backtest."""
    path = _cache_path(key)
    try:
        if path.exists() and time.time() - path.stat().st_mtime <= max_age_seconds:
            with path.open("rb") as fh:
                return pickle.load(fh)
    except Exception:
        pass
    value = loader()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with tmp.open("wb") as fh:
            pickle.dump(value, fh)
        tmp.replace(path)
    except Exception as exc:
        log.warning("Không ghi được cache backtest %s: %s", path.name, exc)
    return value


def _with_quota_retry(fn, *args, **kwargs):
    """VNStock báo quota -> chờ 65 giây rồi thử lại đúng một lần (backtest chạy nền, chờ được)."""
    import stock_service as service

    try:
        return fn(*args, **kwargs)
    except service.VnstockRateLimited:
        log.warning("Backtest chạm quota VNStock, chờ 65 giây rồi thử lại…")
        time.sleep(65)
        return fn(*args, **kwargs)


def validate_prices(symbol: str, df: pd.DataFrame, exchange: str, start: date, min_history: int) -> list[str]:
    """Kiểm tra dữ liệu giá: đủ lịch sử khởi tạo chỉ báo, biến động vượt biên độ (nghi chưa điều chỉnh giá)."""
    notes = []
    if df is None or df.empty:
        return [f"{symbol}: không có dữ liệu giá."]
    before = int((pd.to_datetime(df["time"]).dt.date < start).sum())
    if before < min_history:
        notes.append(f"{symbol}: chỉ có {before} phiên trước ngày bắt đầu (< {min_history}) — "
                     "bộ lọc lịch sử có thể chặn BUY đầu kỳ.")
    limit = PRICE_LIMIT.get(exchange, 0.07)
    close = pd.to_numeric(df["close"], errors="coerce")
    ret = close.pct_change().abs()
    jumps = df.loc[ret > limit + 0.02, "time"]
    if len(jumps):
        days = ", ".join(pd.Timestamp(t).strftime("%d/%m/%Y") for t in jumps.iloc[:3])
        notes.append(f"{symbol}: {len(jumps)} phiên biến động vượt biên độ ±{limit:.0%} ({days}) — "
                     "có thể do chia tách/cổ tức chưa điều chỉnh.")
    return notes


def load_data(symbols, start: date, end: date, cfg: BacktestConfig = BACKTEST, strat_cfg=STRATEGY,
              progress=None) -> BacktestData:
    """Tải OHLCV (kèm warm-up), VN-Index, BCTC và sàn niêm yết cho danh sách mã."""
    import stock_service as service

    symbols = [str(s).upper().strip() for s in symbols if str(s).strip()]
    warm_start = start - timedelta(days=cfg.lookback_days + 10)
    s_iso, e_iso = warm_start.isoformat(), end.isoformat()
    # Khoảng đã khép (END < hôm nay) gần như không đổi -> giữ cache lâu hơn.
    ttl = cfg.cache_hours * 3600 if end >= date.today() else 30 * 86400
    total, done = 2 + 2 * len(symbols), 0
    warnings: list[str] = []
    sources: dict[str, str] = {}

    def step(text: str) -> None:
        nonlocal done
        done += 1
        if progress:
            progress(done, total, text)

    # Sàn niêm yết (để áp biên độ trần/sàn và bộ lọc sàn HOSE/HNX).
    meta: dict[str, dict] = {}
    try:
        listing = service.listing_map()
        meta = {s: listing.get(s) or {} for s in symbols}
    except Exception as exc:
        warnings.append(f"Không tải được danh sách niêm yết ({type(exc).__name__}): giả định các mã thuộc HOSE.")
    for s in symbols:
        m = dict(meta.get(s) or {})
        if m.get("exchange") in (None, "", "UNKNOWN"):
            m["exchange"] = "HOSE"
        meta[s] = m
    step("Danh sách niêm yết")

    try:
        index = _disk_cached(f"bt_idx_VNINDEX_{s_iso}_{e_iso}", ttl, lambda: _with_quota_retry(
            service.fetch_history_range, "VNINDEX", s_iso, e_iso, index=True))
    except Exception as exc:
        raise service.DataUnavailable(f"Không tải được VN-Index cho backtest: {exc}") from exc
    index = service.confirmed_bars(index)
    sources["VNINDEX"] = index.attrs.get("source", "?")
    step("VN-Index")

    prices: dict[str, pd.DataFrame] = {}
    fund_tables: dict[str, dict] = {}
    for s in symbols:
        try:
            df = _disk_cached(f"bt_px_{s}_{s_iso}_{e_iso}", ttl, lambda s=s: _with_quota_retry(
                service.fetch_history_range, s, s_iso, e_iso))
            df = service.confirmed_bars(df)
            prices[s] = df
            sources[s] = df.attrs.get("source", "?")
            warnings += validate_prices(s, df, meta[s]["exchange"], start, strat_cfg.min_history)
        except Exception as exc:
            warnings.append(f"{s}: bỏ khỏi backtest vì không tải được giá ({type(exc).__name__}).")
        step(f"Giá {s}")
        if s not in prices:
            step(f"BCTC {s} (bỏ qua)")
            continue
        try:
            fund_tables[s] = _disk_cached(f"bt_fund_{s}", max(ttl, 86400), lambda s=s: _with_quota_retry(
                lambda: service.get_fundamental_tables(s)[0]))
        except Exception as exc:
            fund_tables[s] = {"source": "?"}
            warnings.append(f"{s}: không tải được BCTC ({type(exc).__name__}) — không thể phát BUY.")
        step(f"BCTC {s}")
    if not prices:
        raise service.DataUnavailable("Không tải được dữ liệu giá của mã nào.")
    return BacktestData(prices=prices, index=index, fund_tables=fund_tables, meta=meta,
                        warnings=warnings, sources=sources)


# =====================================================================
# 2. BCTC theo thời điểm công bố (chống Data Leakage)
# =====================================================================
def quarter_end(year: int, quarter: int) -> date:
    return (pd.Timestamp(year=year, month=3 * quarter, day=1) + pd.offsets.MonthEnd(0)).date()


def available_quarter(day: date, lag_days: int) -> tuple[int, int]:
    """Quý BCTC mới nhất mà nhà đầu tư có thể biết tại ngày `day`."""
    y, q = day.year, (day.month - 1) // 3 + 1
    while quarter_end(y, q) + timedelta(days=lag_days) > day:
        y, q = (y, q - 1) if q > 1 else (y - 1, 4)
    return y, q


class FundamentalAsOf:
    """Snapshot EPS YoY / ROE TTM / D/E "như tại ngày t", cache theo (mã, quý)."""

    def __init__(self, tables: dict[str, dict], lag_days: int):
        self.tables = tables
        self.lag_days = lag_days
        self._cache: dict[tuple, dict] = {}

    def get(self, symbol: str, day: date) -> dict:
        import stock_service as service

        upto = available_quarter(day, self.lag_days)
        key = (symbol, upto)
        if key not in self._cache:
            tables = self.tables.get(symbol) or {"source": "?"}
            try:
                snap = service.snapshot_from_tables(symbol, tables, upto=upto)
            except Exception:
                snap = {"available": False, "latest_period": None, "eps_yoy": None, "eps_status": "missing",
                        "roe": None, "debt_equity": None, "notes": []}
            snap["is_financial"] = symbol in FINANCIAL_SYMBOLS
            self._cache[key] = snap
        return self._cache[key]


# =====================================================================
# 3. Tiện ích giá: bước giá, trần/sàn
# =====================================================================
def tick_size(price: float, exchange: str) -> float:
    if exchange == "HNX":
        return 100.0
    return 10.0 if price < 10_000 else 50.0 if price < 50_000 else 100.0


def at_ceiling(open_price: float, ref: float, exchange: str) -> bool:
    """Giá mở cửa đã chạm trần (giá trần làm tròn xuống theo bước giá nên cho sai số 1 bước)."""
    ceil = ref * (1 + PRICE_LIMIT.get(exchange, 0.07))
    return open_price >= ceil - tick_size(ceil, exchange)


def locked_floor(high: float, ref: float, exchange: str) -> bool:
    """Cả phiên nằm ở giá sàn (trắng bên mua) -> lệnh bán không khớp được."""
    floor = ref * (1 - PRICE_LIMIT.get(exchange, 0.07))
    return high <= floor + tick_size(floor, exchange)


# =====================================================================
# 4. Backtest Engine + Portfolio + Trade Log
# =====================================================================
def _window(times: np.ndarray, day: pd.Timestamp, lookback_days: int) -> tuple[int, int]:
    """[i0, i1) của các nến trong (day - lookback, day]: đúng cửa sổ bot live thấy tại ngày `day`."""
    i1 = int(np.searchsorted(times, day.to_datetime64(), side="right"))
    i0 = int(np.searchsorted(times, (day - pd.Timedelta(days=lookback_days)).to_datetime64(), side="left"))
    return i0, i1


def _bar(df: pd.DataFrame, pos: dict, symbol: str, day: pd.Timestamp):
    i = pos[symbol].get(day)
    return None if i is None else df.iloc[i]


class Replay:
    """Dựng lại ĐÚNG pipeline của bot live tại một ngày t trong quá khứ.

    Live (stock_service.analyze_symbol)            Replay tại ngày t
    ─────────────────────────────────────────────  ────────────────────────────────────────────
    get_history(): 620 ngày lịch tới hôm nay        nến trong (t − 620 ngày, t]
    get_market_gate(): VN-Index tới hôm nay         VN-Index trong (t − 620 ngày, t] -> market_gate
    fundamental_snapshot(): quý mới nhất            quý mới nhất ĐÃ CÔNG BỐ tại t (cuối quý + 45 ngày)
    strategy.compute_technical + evaluate           strategy.compute_technical + evaluate (y hệt)

    Được dùng chung bởi backtest danh mục (run_backtest) và kiểm định tín hiệu (signal_validation).
    """

    def __init__(self, data: "BacktestData", cfg: BacktestConfig = BACKTEST, strat_cfg=STRATEGY):
        self.data, self.cfg, self.strat_cfg = data, cfg, strat_cfg
        self.symbols = list(data.prices)
        self.frames: dict[str, pd.DataFrame] = {}
        self.times: dict[str, np.ndarray] = {}
        self.day_pos: dict[str, dict] = {}
        for s in self.symbols:
            df = data.prices[s].sort_values("time").reset_index(drop=True).copy()
            df["time"] = pd.to_datetime(df["time"]).dt.normalize()
            self.frames[s] = df
            self.times[s] = df["time"].to_numpy()
            self.day_pos[s] = {t: i for i, t in enumerate(df["time"])}
        idx = data.index.sort_values("time").reset_index(drop=True).copy()
        idx["time"] = pd.to_datetime(idx["time"]).dt.normalize()
        self.idx = idx
        self.idx_times = idx["time"].to_numpy()
        self.fundamentals = FundamentalAsOf(data.fund_tables, cfg.fund_lag_days)
        self._gates: dict = {}

    def calendar(self, start: date, end: date) -> list:
        s_ts, e_ts = pd.Timestamp(start), pd.Timestamp(end)
        cal = [t for t in self.idx["time"] if s_ts <= t <= e_ts]
        if not cal:   # thiếu VN-Index: dùng hợp các ngày giao dịch của cổ phiếu
            cal = sorted({t for df in self.frames.values() for t in df["time"] if s_ts <= t <= e_ts})
        return cal

    def gate(self, day: pd.Timestamp) -> tuple[dict, pd.DataFrame]:
        """Cổng thị trường tại ngày t (cache theo ngày) + cửa sổ VN-Index tới t."""
        if day not in self._gates:
            i0, i1 = _window(self.idx_times, day, self.cfg.lookback_days)
            index_win = self.idx.iloc[i0:i1]
            self._gates[day] = (strategy.market_gate(index_win, self.strat_cfg), index_win)
        return self._gates[day]

    def has_bar(self, symbol: str, day: pd.Timestamp) -> bool:
        return day in self.day_pos[symbol]

    def evaluate(self, symbol: str, day: pd.Timestamp, position: dict | None = None):
        """(tech, fund, kết quả evaluate) của `symbol` tại cuối phiên `day`. Có thể ném lỗi tính chỉ báo."""
        gate, index_win = self.gate(day)
        j0, j1 = _window(self.times[symbol], day, self.cfg.lookback_days)
        if j1 - j0 < self.strat_cfg.min_history:   # giống bot live: thiếu phiên thì lấy cửa sổ dài hơn
            j0, _ = _window(self.times[symbol], day, 1500)
        win = self.frames[symbol].iloc[j0:j1]
        tech = strategy.compute_technical(win, index_win, self.strat_cfg)
        fund = self.fundamentals.get(symbol, day.date())
        r = strategy.evaluate(symbol, tech, fund, gate, self.data.meta[symbol], position=position,
                              cfg=self.strat_cfg)
        return tech, fund, r


def run_backtest(data: BacktestData, start: date, end: date, cfg: BacktestConfig = BACKTEST,
                 strat_cfg=STRATEGY, progress=None) -> dict:
    """Mô phỏng danh mục ngày-qua-ngày. Trả về dict gồm equity, trade log, round trips, metrics."""
    t0 = time.time()
    warnings = list(data.warnings)
    if cfg.use_margin:
        warnings.append("USE_MARGIN=True chưa được hỗ trợ: backtest vẫn chạy KHÔNG margin.")

    replay = Replay(data, cfg, strat_cfg)
    symbols, frames, day_pos, idx = replay.symbols, replay.frames, replay.day_pos, replay.idx
    s_ts, e_ts = pd.Timestamp(start), pd.Timestamp(end)
    calendar = replay.calendar(start, end)
    if len(calendar) < 2:
        raise ValueError("Khoảng backtest có ít hơn 2 phiên giao dịch.")
    cal_pos = {t: i for i, t in enumerate(calendar)}
    cash = float(cfg.initial_capital)
    positions: dict[str, dict] = {}            # mã -> vị thế đang giữ
    pending_sells: dict[str, dict] = {}        # mã -> lệnh bán chờ khớp
    pending_buys: list[dict] = []              # lệnh mua cho phiên kế tiếp
    orders: list[dict] = []                    # Trade Log (cả lệnh khớp lẫn bị bỏ qua)
    trips: list[dict] = []                     # giao dịch khứ hồi đã đóng
    equity_rows: list[dict] = []
    last_close: dict[str, float] = {}
    signal_counts = {"BUY": 0, "SELL": 0}
    diag: list[dict] = []                      # nhật ký tín hiệu: mỗi (phiên, mã) một dòng -> chẩn đoán
    calc_errors: dict[str, int] = {}
    gate_days: dict[str, int] = {}
    fee, tax, lot = cfg.transaction_fee, cfg.sell_tax, cfg.lot_size

    def equity_now() -> float:
        return cash + sum(p["qty"] * last_close.get(s, p["entry_price"]) for s, p in positions.items())

    def log_order(**row) -> None:
        base = {"status": "KHỚP", "symbol": None, "side": None, "signal_date": None, "signal": None,
                "signal_price": None, "exec_date": None, "exec_price": None, "qty": 0, "value": 0.0,
                "fee": 0.0, "tax": 0.0, "pnl": None, "pnl_pct": None, "holding_sessions": None,
                "reason": "", "score": None, "market": None}
        base.update(row)
        orders.append(base)

    def close_position(s: str, day: pd.Timestamp, price: float, order: dict, reason: str) -> None:
        nonlocal cash
        p = positions.pop(s)
        value = p["qty"] * price
        f, tx = value * fee, value * tax
        cash += value - f - tx
        cost = p["cost"]
        pnl = value - f - tx - cost
        sessions = cal_pos.get(day, len(calendar) - 1) - cal_pos.get(p["entry_date"], 0)
        log_order(symbol=s, side="BÁN", signal_date=order.get("signal_date"), signal=order.get("signal", "SELL"),
                  signal_price=order.get("signal_price"), exec_date=day, exec_price=price, qty=p["qty"],
                  value=value, fee=f, tax=tx, pnl=pnl, pnl_pct=pnl / cost * 100, holding_sessions=sessions,
                  reason=reason, market=order.get("market"))
        trips.append({"symbol": s, "entry_date": p["entry_date"], "exit_date": day, "entry_price": p["entry_price"],
                      "exit_price": price, "qty": p["qty"], "cost": cost, "proceeds": value - f - tx, "pnl": pnl,
                      "pnl_pct": pnl / cost * 100, "holding_sessions": sessions,
                      "holding_days": (day - p["entry_date"]).days, "exit_reason": reason})

    for k, day in enumerate(calendar):
        # ---- (1) Khớp lệnh tại giá MỞ CỬA phiên hôm nay cho tín hiệu của phiên trước (t -> t+1)
        for s, order in list(pending_sells.items()):
            b = _bar(frames[s], day_pos, s, day)
            if b is None or s not in positions:
                continue                                         # mã không giao dịch hôm nay: chờ phiên sau
            if cal_pos[day] - cal_pos.get(positions[s]["entry_date"], 0) < cfg.settlement_days:
                continue                                         # cổ phiếu chưa về tài khoản (T+2)
            ref = last_close.get(s, to_float(b["close"]))
            px_open = to_float(b["open"], to_float(b["close"]))
            px_high = to_float(b["high"], px_open)
            if px_open is None:
                continue
            if ref is not None and locked_floor(px_high, ref, data.meta[s]["exchange"]):
                order["floor_waits"] = order.get("floor_waits", 0) + 1
                continue                                         # trắng bên mua tại giá sàn
            reason = order["reason"] + (f" (chờ {order['floor_waits']} phiên sàn)" if order.get("floor_waits") else "")
            close_position(s, day, px_open, order, reason)
            pending_sells.pop(s)

        for order in pending_buys:
            s = order["symbol"]
            skip = None
            b = _bar(frames[s], day_pos, s, day)
            exch = data.meta[s]["exchange"]
            # Kiểm tra khả năng khớp theo giá trước, rồi mới tới giới hạn danh mục.
            if b is None or to_float(b["open"]) is None:
                skip = "Không có giao dịch phiên t+1"
            else:
                o = to_float(b["open"])
                if at_ceiling(o, order["signal_price"], exch):
                    skip = "Open t+1 ở giá TRẦN: không giả định khớp lệnh"
                elif order["pivot"] is not None and o <= order["pivot"]:
                    skip = f"Open {fmt_price(o)} ≤ Pivot {fmt_price(order['pivot'])}"
                elif order["buy_zone"] is not None and o > order["buy_zone"]:
                    skip = f"Open {fmt_price(o)} vượt vùng mua {fmt_price(order['buy_zone'])}"
                elif s in positions:
                    skip = "Đã có vị thế"
                elif len(positions) >= cfg.max_positions:
                    skip = f"Đã đủ {cfg.max_positions} vị thế"
            if skip is None:
                o = to_float(b["open"])
                target = order["equity"] / cfg.max_positions * (order["weight_factor"] if cfg.apply_weight_factor else 1)
                budget = min(target, cash)
                qty = int(budget / (o * (1 + fee)) // lot) * lot
                if qty < lot:
                    skip = "Không đủ tiền mặt cho 1 lô"
                else:
                    value = qty * o
                    f = value * fee
                    cash -= value + f
                    positions[s] = {"symbol": s, "qty": qty, "entry_price": o, "entry_date": day,
                                    "peak_close": o, "cost": value + f}
                    log_order(symbol=s, side="MUA", signal_date=order["signal_date"], signal="BUY",
                              signal_price=order["signal_price"], exec_date=day, exec_price=o, qty=qty, value=value,
                              fee=f, reason=order["reason"], score=order["score"], market=order["market"])
            if skip:
                log_order(status="BỎ QUA", symbol=s, side="MUA", signal_date=order["signal_date"], signal="BUY",
                          signal_price=order["signal_price"], exec_date=day,
                          exec_price=to_float(b["open"]) if b is not None else None,
                          reason=skip, score=order["score"], market=order["market"])
        pending_buys = []

        # ---- (2) Định giá danh mục theo giá đóng cửa
        for s in symbols:
            b = _bar(frames[s], day_pos, s, day)
            if b is not None and to_float(b["close"]) is not None:
                last_close[s] = to_float(b["close"])
        pos_value = sum(p["qty"] * last_close.get(s, p["entry_price"]) for s, p in positions.items())
        equity_rows.append({"date": day, "cash": cash, "positions_value": pos_value, "equity": cash + pos_value,
                            "n_positions": len(positions)})
        if progress:
            progress(k + 1, len(calendar), day.strftime("%d/%m/%Y"))
        # Phiên cuối vẫn chấm điểm (để đối chiếu với /signal) nhưng KHÔNG đặt lệnh vì không còn phiên t+1.
        final = k == len(calendar) - 1

        # ---- (3) Tín hiệu cuối ngày t: chỉ dùng dữ liệu <= t
        gate, _ = replay.gate(day)
        gate_days[gate["state"]] = gate_days.get(gate["state"], 0) + 1
        eq_signal = equity_now()
        buys = []
        for s in symbols:
            if day not in day_pos[s]:
                diag.append({"date": day, "symbol": s, "market": gate["state"], "signal": "KHÔNG GIAO DỊCH",
                             "final": final})
                continue
            if s in pending_sells:
                diag.append({"date": day, "symbol": s, "market": gate["state"], "signal": "CHỜ BÁN",
                             "final": final, "reason": pending_sells[s]["reason"]})
                continue
            pos = positions.get(s)
            try:
                tech, fund, r = replay.evaluate(s, day, pos)
            except Exception as exc:
                calc_errors[s] = calc_errors.get(s, 0) + 1
                diag.append({"date": day, "symbol": s, "market": gate["state"], "signal": "LỖI",
                             "final": final, "reason": f"{type(exc).__name__}: {exc}"[:200]})
                continue
            diag.append(_diag_row(day, s, gate, tech, fund, r, final))
            if final:
                continue
            if r["signal"] == "SELL" and pos:
                signal_counts["SELL"] += 1
                pending_sells[s] = {"signal_date": day, "signal": "SELL", "signal_price": tech.get("close"),
                                    "reason": "; ".join(r["reasons"]), "market": gate["state"]}
            elif r["signal"] == "BUY":
                signal_counts["BUY"] += 1
                buys.append({"symbol": s, "signal_date": day, "signal_price": tech.get("close"),
                             "score": r["score"], "delta": tech.get("delta_roc63"),
                             "pivot": r.get("breakout_pivot"), "buy_zone": r.get("buy_zone"),
                             "weight_factor": float(gate.get("weight_factor") or 0.0), "equity": eq_signal,
                             "market": gate["state"],
                             "reason": f"Điểm {r['score']}/100 ≥ {r['threshold']:g} · {gate['state']}"})
        # Nhiều BUY cùng lúc: ưu tiên điểm cao, rồi Delta ROC63 cao (giống /top).
        buys.sort(key=lambda o: (-(o["score"] or 0), -(o["delta"] if o["delta"] is not None else -1e9), o["symbol"]))
        pending_buys = buys

    # ---- Cuối kỳ
    last_day = calendar[-1]
    for order in pending_buys:
        log_order(status="BỎ QUA", symbol=order["symbol"], side="MUA", signal_date=order["signal_date"], signal="BUY",
                  signal_price=order["signal_price"], reason="Tín hiệu cuối kỳ, không còn phiên t+1",
                  score=order["score"], market=order["market"])
    open_positions = []
    if cfg.close_at_end:
        for s in list(positions):
            order = pending_sells.pop(s, None) or {"signal_date": last_day, "signal": "CUỐI KỲ",
                                                   "signal_price": last_close.get(s)}
            close_position(s, last_day, last_close.get(s, positions[s]["entry_price"]), order,
                           "Đóng cuối kỳ theo giá đóng cửa (định giá, không phải tín hiệu)")
        equity_rows[-1].update(cash=cash, positions_value=0.0, equity=cash, n_positions=0)
    else:
        open_positions = [{"symbol": s, **p} for s, p in positions.items()]

    equity = pd.DataFrame(equity_rows)
    bench = idx[(idx["time"] >= s_ts) & (idx["time"] <= e_ts)][["time", "close"]].rename(columns={"time": "date"})
    equity = equity.merge(bench.rename(columns={"close": "vnindex"}), on="date", how="left")
    equity["vnindex"] = equity["vnindex"].ffill().bfill()

    for s, n in calc_errors.items():
        warnings.append(f"{s}: {n} phiên không tính được chỉ báo (xem cột ly_do trong nhật ký tín hiệu).")

    report = {
        "symbols": symbols,
        "start": calendar[0].date(), "end": last_day.date(),
        "requested_start": start, "requested_end": end,
        "sessions": len(calendar),
        "config": {k: getattr(cfg, k) for k in (
            "initial_capital", "max_positions", "use_margin", "sell_tax", "transaction_fee", "lot_size",
            "settlement_days", "fund_lag_days", "lookback_days", "apply_weight_factor", "close_at_end")},
        "equity": equity,
        "orders": orders,
        "trips": trips,
        "open_positions": open_positions,
        "signal_counts": signal_counts,
        "signal_log": diag,
        "gate_days": gate_days,
        "warnings": warnings,
        "sources": data.sources,
        "runtime_seconds": round(time.time() - t0, 1),
    }
    report["metrics"] = compute_metrics(report)
    report["diagnostics"] = compute_diagnostics(report, strat_cfg)
    return report


# =====================================================================
# 4b. Chẩn đoán: vì sao có / không có lệnh
# =====================================================================
CHECK_KEYS = ("market", "hard_filter", "fundamental_source", "mandatory_breakout", "score")
CHECK_LABELS = {"market": "Thị trường", "hard_filter": "Bộ lọc cứng", "fundamental_source": "BCTC",
                "mandatory_breakout": "Breakout", "score": "Đủ điểm", "no_position": "Chưa giữ"}


def _diag_row(day, symbol: str, gate: dict, tech: dict, fund: dict, r: dict, final: bool) -> dict:
    checks = r.get("checks") or {}
    hard = (r.get("hard") or {}).get("checks") or {}
    return {
        "date": day, "symbol": symbol, "market": gate.get("state"), "final": final,
        "signal": r.get("signal"), "hold_type": r.get("hold_type"),
        "score": r.get("score_min"), "score_max": r.get("score_max"), "threshold": r.get("threshold"),
        "t1": (r.get("tier1") or {}).get("score"), "t2": (r.get("tier2") or {}).get("score_min"),
        "t3": (r.get("tier3") or {}).get("score"),
        **{f"ok_{k}": bool(checks.get(k)) for k in (*CHECK_KEYS, "no_position")},
        "hard_fail_keys": [k for k, v in hard.items() if not v.get("ok")],
        "hard_fail": "; ".join(v["detail"] for v in hard.values() if not v.get("ok")),
        "close": tech.get("close"), "adv20": tech.get("adv20"), "debt_equity": fund.get("debt_equity"),
        "history": tech.get("history_count"), "fund_period": fund.get("latest_period"),
        "reason": " | ".join(r.get("reasons") or []),
    }


def _hard_fail_text(key: str, row: dict, strat_cfg) -> str:
    """Diễn giải điều kiện bộ lọc cứng bị trượt, kèm giá trị ở phiên gần nhất."""
    if key == "debt":
        de = to_float(row.get("debt_equity"))
        if de is None:
            return "Không đọc được nợ vay/VCSH từ BCTC"
        return f"Nợ vay/VCSH > {strat_cfg.max_debt_equity:g} (gần nhất {de:.2f})"
    if key == "liquidity":
        adv = to_float(row.get("adv20"))
        return (f"GTGD TB 20 phiên < {strat_cfg.min_adv_value / 1e9:g} tỷ"
                + ("" if adv is None else f" (gần nhất {adv / 1e9:.1f} tỷ)"))
    if key == "history":
        return f"Lịch sử < {strat_cfg.min_history} phiên (có {row.get('history')})"
    if key == "exchange":
        return "Không thuộc HOSE/HNX"
    if key == "status":
        return "Nằm trong BLOCKED_SYMBOLS"
    return key


def compute_diagnostics(report: dict, strat_cfg=STRATEGY) -> dict:
    """Đếm, cho từng mã, số phiên mỗi điều kiện BUY bị trượt; tìm phiên "gần BUY nhất" và điểm cao nhất."""
    from collections import Counter

    per_symbol = {}
    for s in report["symbols"]:
        rows = [d for d in report["signal_log"] if d["symbol"] == s and "ok_market" in d]
        free = [d for d in rows if d["ok_no_position"]]           # phiên chưa giữ mã -> xét được BUY
        info = {
            "sessions": len([d for d in report["signal_log"] if d["symbol"] == s]),
            "evaluated": len(rows), "free": len(free), "held": len(rows) - len(free),
            "buy_days": sum(d["signal"] == "BUY" and not d["final"] for d in rows),
            "avoid_days": sum(d.get("hold_type") == "TRÁNH MUA" for d in free),
            "errors": sum(d["signal"] == "LỖI" for d in report["signal_log"] if d["symbol"] == s),
            "fail": {k: sum(not d[f"ok_{k}"] for d in free) for k in CHECK_KEYS},
            "hard_reasons": [], "main_blocker": None, "best": None, "max_score": None, "last": None,
        }
        hard_counter = Counter(k for d in free for k in d["hard_fail_keys"])
        latest_row = {}
        for d in free:
            for k in d["hard_fail_keys"]:
                latest_row[k] = d
        info["hard_reasons"] = [(_hard_fail_text(k, latest_row[k], strat_cfg), n) for k, n in hard_counter.most_common()]
        if free:
            best_score = max(free, key=lambda d: (d["score"] or 0))
            info["max_score"] = {"score": best_score["score"], "date": best_score["date"],
                                 "threshold": best_score["threshold"]}

            def closeness(d):
                return sum(d[f"ok_{k}"] for k in CHECK_KEYS), d["score"] or 0
            b = max(free, key=closeness)
            # +1 = điều kiện "chưa giữ" (luôn đạt ở các phiên xét mua) -> cùng thang 6 điều kiện với thẻ /signal.
            info["best"] = {"date": b["date"], "passed": closeness(b)[0] + 1, "total": len(CHECK_KEYS) + 1,
                            "score": b["score"], "threshold": b["threshold"], "signal": b["signal"],
                            "missing": [CHECK_LABELS[k] for k in CHECK_KEYS if not b[f"ok_{k}"]]}
            # Nút thắt chính: trong các phiên thị trường CHO mua, điều kiện nào trượt nhiều nhất.
            open_days = [d for d in free if d["ok_market"]]
            if not open_days:
                info["main_blocker"] = ("market", len(free), len(free))
            else:
                counts = {k: sum(not d[f"ok_{k}"] for d in open_days) for k in CHECK_KEYS[1:]}
                key = max(counts, key=lambda k: counts[k])
                info["main_blocker"] = (key, counts[key], len(open_days)) if counts[key] else None
        last = [d for d in report["signal_log"] if d["symbol"] == s and d["final"]]
        if last:
            info["last"] = last[-1]
        per_symbol[s] = info
    gate_days = report.get("gate_days") or {}
    blocked = sum(v for k, v in gate_days.items() if k not in ("UPTREND", "PRESSURE"))
    return {"per_symbol": per_symbol, "market_blocked": blocked, "market_total": sum(gate_days.values())}


def signal_log_frame(report: dict) -> pd.DataFrame:
    """Nhật ký tín hiệu dạng DataFrame (cột date, symbol, close, label, score, threshold, market) để vẽ."""
    rows = [d for d in report.get("signal_log") or [] if d.get("close") is not None]
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["label"] = [sig if sig in ("BUY", "SELL") else (ht or "CHỜ") for sig, ht in zip(df["signal"], df["hold_type"])]
    return df


SIGNAL_LOG_COLUMNS = [
    ("date", "ngay"), ("symbol", "ma"), ("market", "thi_truong"), ("signal", "tin_hieu"), ("hold_type", "loai_hold"),
    ("score", "diem"), ("score_max", "diem_toi_da"), ("threshold", "nguong_buy"), ("t1", "T1"), ("t2", "T2"),
    ("t3", "T3"), ("ok_market", "dk_thi_truong"), ("ok_hard_filter", "dk_bo_loc"),
    ("ok_fundamental_source", "dk_bctc"), ("ok_mandatory_breakout", "dk_breakout"), ("ok_score", "dk_du_diem"),
    ("ok_no_position", "dk_chua_giu"), ("hard_fail", "bo_loc_truot"), ("close", "gia_dong_cua"),
    ("fund_period", "quy_bctc_dung"), ("final", "phien_cuoi_chi_doi_chieu"), ("reason", "ly_do"),
]


def signal_log_csv(report: dict) -> bytes:
    """Nhật ký tín hiệu từng phiên × từng mã: kiểm tra được vì sao mỗi ngày có/không có BUY."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([name for _, name in SIGNAL_LOG_COLUMNS])
    for d in report.get("signal_log") or []:
        row = []
        for key, _ in SIGNAL_LOG_COLUMNS:
            v = d.get(key)
            if key == "date":
                v = _d(v)
            elif isinstance(v, bool):
                v = "x" if key == "final" and v else ("" if key == "final" else ("✓" if v else "✗"))
            elif isinstance(v, float):
                v = round(v, 2)
            row.append("" if v is None else v)
        w.writerow(row)
    return buf.getvalue().encode("utf-8-sig")


# =====================================================================
# 5. Metrics
# =====================================================================
def max_drawdown(values) -> dict:
    """MDD (%, số âm) + ngày đỉnh/đáy. `values` là Series có index ngày."""
    v = pd.Series(values, dtype=float).dropna()
    if v.empty:
        return {"mdd": None, "peak": None, "trough": None}
    dd = v / v.cummax() - 1
    trough = dd.idxmin()
    peak = v.loc[:trough].idxmax()
    return {"mdd": float(dd.min() * 100), "peak": peak, "trough": trough}


def compute_metrics(report: dict) -> dict:
    eq = report["equity"].set_index("date")
    trips = report["trips"]
    capital = float(report["config"]["initial_capital"])
    final = float(eq["equity"].iloc[-1])
    pnls = [t["pnl"] for t in trips]
    wins = [t for t in trips if t["pnl"] > 0]
    losses = [t for t in trips if t["pnl"] <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in losses)
    vni = eq["vnindex"]
    vni_ret = (float(vni.iloc[-1]) / float(vni.iloc[0]) - 1) * 100 if vni.notna().all() and vni.iloc[0] else None
    dd = max_drawdown(eq["equity"])
    vdd = max_drawdown(vni) if vni.notna().any() else {"mdd": None}
    filled = [o for o in report["orders"] if o["status"] == "KHỚP"]
    skipped = [o for o in report["orders"] if o["status"] == "BỎ QUA"]
    skip_groups: dict[str, int] = {}
    for o in skipped:
        r = o["reason"]
        key = ("Giá trần" if "TRẦN" in r else "≤ Pivot" if "Pivot" in r else "Vượt vùng mua" if "vùng mua" in r
               else "Đủ vị thế" if "vị thế" in r else "Thiếu tiền" if "tiền" in r else "Cuối kỳ" if "cuối kỳ" in r
               else "Khác")
        skip_groups[key] = skip_groups.get(key, 0) + 1
    total_return = (final / capital - 1) * 100
    return {
        "initial_capital": capital,
        "final_equity": final,
        "total_return": total_return,
        "vnindex_return": vni_ret,
        "excess_return": None if vni_ret is None else total_return - vni_ret,
        "n_trades": len(trips),
        "n_wins": len(wins),
        "n_losses": len(losses),
        "win_rate": len(wins) / len(trips) * 100 if trips else None,
        "avg_win_pct": float(np.mean([t["pnl_pct"] for t in wins])) if wins else None,
        "avg_loss_pct": float(np.mean([t["pnl_pct"] for t in losses])) if losses else None,
        "avg_win": gross_win / len(wins) if wins else None,
        "avg_loss": -gross_loss / len(losses) if losses else None,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else (math.inf if gross_win > 0 else None),
        "net_pnl": sum(pnls),
        "max_drawdown": dd["mdd"],
        "mdd_peak": dd["peak"], "mdd_trough": dd["trough"],
        "vnindex_mdd": vdd["mdd"],
        "avg_holding_sessions": float(np.mean([t["holding_sessions"] for t in trips])) if trips else None,
        "avg_holding_days": float(np.mean([t["holding_days"] for t in trips])) if trips else None,
        "total_fees": sum(o["fee"] for o in filled),
        "total_tax": sum(o["tax"] for o in filled),
        "n_orders": len(filled),
        "n_skipped": len(skipped),
        "skipped_by_reason": skip_groups,
        "avg_exposure": float((eq["positions_value"] / eq["equity"]).mean() * 100),
        "best_trade": max(trips, key=lambda t: t["pnl_pct"]) if trips else None,
        "worst_trade": min(trips, key=lambda t: t["pnl_pct"]) if trips else None,
    }


# =====================================================================
# 6. Report / Chart / Trade Log
# =====================================================================
def render_report_png(report: dict) -> bytes:
    """Ảnh 3 tầng: Equity Curve vs VN-Index | Drawdown | Tỷ lệ vốn đang đầu tư."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib.ticker import FuncFormatter, MaxNLocator

    from chart_service import COLORS, _render_lock, _style_axis

    eq = report["equity"].reset_index(drop=True)
    m = report["metrics"]
    capital = m["initial_capital"]
    n = len(eq)
    x = np.arange(n)
    port = eq["equity"].to_numpy(float) / 1e6
    vni = (eq["vnindex"] / eq["vnindex"].iloc[0] * capital / 1e6).to_numpy(float)
    dd_port = (eq["equity"] / eq["equity"].cummax() - 1).to_numpy(float) * 100
    dd_vni = (eq["vnindex"] / eq["vnindex"].cummax() - 1).to_numpy(float) * 100
    has_vni = bool(np.isfinite(vni).any())
    expo = (eq["positions_value"] / eq["equity"]).to_numpy(float) * 100
    pos_of = {d: i for i, d in enumerate(eq["date"])}
    buys = [pos_of[o["exec_date"]] for o in report["orders"]
            if o["status"] == "KHỚP" and o["side"] == "MUA" and o["exec_date"] in pos_of]
    sells = [pos_of[o["exec_date"]] for o in report["orders"]
             if o["status"] == "KHỚP" and o["side"] == "BÁN" and o["exec_date"] in pos_of]

    with _render_lock:
        fig = Figure(figsize=(12, 7.6), dpi=140, facecolor=COLORS["bg"])
        FigureCanvasAgg(fig)
        grid = fig.add_gridspec(3, 1, height_ratios=[4.2, 1.8, 1.3], hspace=0.08,
                                left=0.045, right=0.915, top=0.86, bottom=0.07)
        ax1 = fig.add_subplot(grid[0])
        ax2 = fig.add_subplot(grid[1], sharex=ax1)
        ax3 = fig.add_subplot(grid[2], sharex=ax1)
        for ax in (ax1, ax2, ax3):
            _style_axis(ax)

        ax1.axhline(capital / 1e6, color=COLORS["muted"], linewidth=0.8, linestyle=(0, (3, 3)))
        if has_vni:
            ax1.plot(x, vni, color=COLORS["ma50"], linewidth=1.4, label="VN-Index (quy đổi cùng vốn)")
        ax1.plot(x, port, color=COLORS["ma20"], linewidth=2.0, label="Danh mục Backtest")
        if buys:
            ax1.scatter(buys, port[buys], marker="^", s=38, color=COLORS["up"], zorder=5, label="Mua")
        if sells:
            ax1.scatter(sells, port[sells], marker="v", s=38, color=COLORS["down"], zorder=5, label="Bán")
        ax1.legend(loc="upper left", frameon=False, fontsize=8.5, labelcolor=COLORS["text"], ncol=4)
        ax1.yaxis.set_major_formatter(FuncFormatter(lambda v, _p: f"{v:,.0f}tr"))
        ax1.text(0.995, 0.03, "Equity Curve (triệu VND)", transform=ax1.transAxes, ha="right",
                 fontsize=8, color=COLORS["muted"])

        ax2.fill_between(x, dd_port, 0, color=COLORS["down"], alpha=0.35, label="Danh mục")
        if has_vni:
            ax2.plot(x, dd_vni, color=COLORS["ma50"], linewidth=1.0, label="VN-Index")
        ax2.yaxis.set_major_formatter(FuncFormatter(lambda v, _p: f"{v:.0f}%"))
        ax2.legend(loc="lower left", frameon=False, fontsize=8, labelcolor=COLORS["text"], ncol=2)
        ax2.text(0.995, 0.06, "Drawdown", transform=ax2.transAxes, ha="right", fontsize=8, color=COLORS["muted"])
        low = min(np.nanmin(dd_port), np.nanmin(dd_vni) if has_vni else 0.0, -1.0)
        ax2.set_ylim(low * 1.15, 0.5)

        ax3.fill_between(x, expo, 0, step="mid", color=COLORS["ma150"], alpha=0.35)
        ax3.set_ylim(0, 105)
        ax3.yaxis.set_major_formatter(FuncFormatter(lambda v, _p: f"{v:.0f}%"))
        ax3.text(0.995, 0.8, "Tỷ lệ vốn đang đầu tư", transform=ax3.transAxes, ha="right",
                 fontsize=8, color=COLORS["muted"])

        dates = pd.to_datetime(eq["date"])
        ax3.xaxis.set_major_locator(MaxNLocator(8, integer=True))
        ax3.xaxis.set_major_formatter(FuncFormatter(
            lambda v, _p: dates.iloc[int(round(v))].strftime("%d/%m/%y") if 0 <= round(v) < n else ""))
        ax1.tick_params(labelbottom=False)
        ax2.tick_params(labelbottom=False)
        ax1.set_xlim(-1, n)

        ret, vret = m["total_return"], m["vnindex_return"]
        color = COLORS["up"] if ret >= 0 else COLORS["down"]
        fig.text(0.045, 0.95, f"BACKTEST · {len(report['symbols'])} mã", fontsize=16, fontweight="bold",
                 color=COLORS["text"], va="center")
        fig.text(0.32, 0.95, f"{ret:+.2f}%", fontsize=16, fontweight="bold", color=color, va="center")
        vtxt = "N/A" if vret is None else f"{vret:+.2f}%"
        fig.text(0.045, 0.905,
                 f"{report['start']:%d/%m/%Y} → {report['end']:%d/%m/%Y} · {report['sessions']} phiên · "
                 f"VN-Index {vtxt} · MDD {_pct(m['max_drawdown'])} · Win rate {_pct(m['win_rate'], 0)} · "
                 f"{m['n_trades']} giao dịch · PF {_pf(m['profit_factor'])}",
                 fontsize=9, color=COLORS["muted"], va="center")
        fig.text(0.915, 0.02, "Kết quả quá khứ không đảm bảo lợi nhuận tương lai", fontsize=7,
                 color=COLORS["muted"], ha="right")
        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=COLORS["bg"])
    return buf.getvalue()


def _pct(x, digits: int = 1) -> str:
    x = to_float(x)
    return "N/A" if x is None else f"{x:.{digits}f}%"


def _pf(x) -> str:
    if x is None:
        return "N/A"
    return "∞" if math.isinf(x) else f"{x:.2f}"


def _d(value) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return pd.Timestamp(value).strftime("%Y-%m-%d")


TRADE_LOG_COLUMNS = [
    ("status", "trang_thai"), ("symbol", "ma"), ("side", "chieu"), ("signal_date", "ngay_tin_hieu"),
    ("signal", "loai_tin_hieu"), ("signal_price", "gia_tin_hieu"), ("exec_date", "ngay_thuc_hien"),
    ("exec_price", "gia_thuc_hien"), ("qty", "so_luong"), ("value", "gia_tri"), ("fee", "phi_giao_dich"),
    ("tax", "thue_ban"), ("pnl", "lai_lo"), ("pnl_pct", "lai_lo_pct"), ("holding_sessions", "so_phien_nam_giu"),
    ("score", "diem"), ("market", "thi_truong"), ("reason", "ly_do"),
]


def trade_log_csv(report: dict) -> bytes:
    """Trade Log dạng CSV (UTF-8 có BOM để Excel đọc đúng tiếng Việt)."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([name for _, name in TRADE_LOG_COLUMNS])
    for o in report["orders"]:
        row = []
        for key, _ in TRADE_LOG_COLUMNS:
            v = o.get(key)
            if key in ("signal_date", "exec_date"):
                v = _d(v)
            elif isinstance(v, float):
                v = round(v, 2)
            row.append("" if v is None else v)
        w.writerow(row)
    return buf.getvalue().encode("utf-8-sig")


def equity_csv(report: dict) -> bytes:
    eq = report["equity"].copy()
    eq["date"] = pd.to_datetime(eq["date"]).dt.strftime("%Y-%m-%d")
    eq["drawdown_pct"] = (eq["equity"] / eq["equity"].cummax() - 1) * 100
    return eq.round(2).to_csv(index=False).encode("utf-8-sig")


def summary_text(report: dict) -> str:
    """Tóm tắt dạng chữ thường cho CLI / file summary.txt."""
    m = report["metrics"]
    lines = [
        f"BACKTEST {report['start']} -> {report['end']} ({report['sessions']} phiên), {len(report['symbols'])} mã",
        f"Mã: {', '.join(report['symbols'])}",
        f"Vốn: {m['initial_capital']:,.0f} -> {m['final_equity']:,.0f} VND",
        f"Total Return      : {_pct(m['total_return'], 2)}   (VN-Index {_pct(m['vnindex_return'], 2)}, "
        f"chênh {_pct(m['excess_return'], 2)})",
        f"Number of Trades  : {m['n_trades']}  (thắng {m['n_wins']}, thua {m['n_losses']})",
        f"Win Rate          : {_pct(m['win_rate'])}",
        f"Average Win/Loss  : {_pct(m['avg_win_pct'], 2)} / {_pct(m['avg_loss_pct'], 2)}",
        f"Profit Factor     : {_pf(m['profit_factor'])}",
        f"Maximum Drawdown  : {_pct(m['max_drawdown'], 2)}  (VN-Index {_pct(m['vnindex_mdd'], 2)})",
        f"Avg Holding Period: {m['avg_holding_sessions'] or 0:.1f} phiên (~{m['avg_holding_days'] or 0:.0f} ngày)",
        f"Phí + thuế        : {m['total_fees'] + m['total_tax']:,.0f} VND",
        f"Tín hiệu BUY/SELL : {report['signal_counts']['BUY']}/{report['signal_counts']['SELL']}; "
        f"lệnh bị bỏ qua {m['n_skipped']} {m['skipped_by_reason']}",
        f"Thời gian chạy    : {report['runtime_seconds']} giây",
    ]
    diag = report.get("diagnostics") or {}
    if diag:
        lines += ["", f"Chẩn đoán: thị trường chặn mua mới {diag['market_blocked']}/{diag['market_total']} phiên"]
        for s, info in diag["per_symbol"].items():
            best = info.get("best") or {}
            mx = info.get("max_score") or {}
            last = info.get("last") or {}
            lines.append(
                f"- {s}: BUY {info['buy_days']} phiên; trượt bộ lọc {info['fail']['hard_filter']}, "
                f"không breakout {info['fail']['mandatory_breakout']}, thiếu điểm {info['fail']['score']} "
                f"/ {info['free']} phiên xét mua; điểm cao nhất {mx.get('score')}"
                + (f" ({_d(mx.get('date'))})" if mx else "")
                + (f"; gần BUY nhất {_d(best.get('date'))} thiếu {', '.join(best.get('missing') or []) or '-'}"
                   if best else "")
                + (f"; phiên cuối {last.get('signal')} {last.get('score')}/100" if last.get("score") is not None else ""))
            for text, n in info["hard_reasons"]:
                lines.append(f"    bộ lọc: {text} — {n} phiên")
    if report["warnings"]:
        lines += ["", "Cảnh báo dữ liệu:"] + [f"- {w}" for w in report["warnings"]]
    return "\n".join(lines)


def save_report(report: dict, folder: Path | None = None) -> Path:
    """Ghi summary.txt, trade_log.csv, equity.csv, signal_log.csv, backtest.png vào data/backtest_reports/<thời điểm>/."""
    folder = folder or REPORT_DIR / datetime.now().strftime("%Y%m%d_%H%M%S")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "summary.txt").write_text(summary_text(report), encoding="utf-8")
    (folder / "trade_log.csv").write_bytes(trade_log_csv(report))
    (folder / "equity.csv").write_bytes(equity_csv(report))
    (folder / "signal_log.csv").write_bytes(signal_log_csv(report))
    (folder / "backtest.png").write_bytes(render_report_png(report))
    return folder


# =====================================================================
# 7. API dùng chung cho bot và CLI
# =====================================================================
_run_lock = threading.Lock()
_result_cache: dict[str, tuple[float, dict]] = {}
RESULT_CACHE_SECONDS = 1800


def run(symbols=None, start=None, end=None, months: int | None = None, cfg: BacktestConfig = BACKTEST,
        progress=None, use_cache: bool = True) -> dict:
    """Chạy trọn quy trình Load -> Signals -> Simulate -> Metrics. Mỗi lúc chỉ một backtest.

    progress(stage, done, total, text): stage là "data" hoặc "sim".
    """
    symbols = [s.upper() for s in (symbols or cfg.symbols)]
    s, e = resolve_period(start, end, months, cfg)
    key = json.dumps([symbols, s.isoformat(), e.isoformat(), repr(cfg), repr(STRATEGY)])
    hit = _result_cache.get(key)
    if use_cache and hit and time.time() - hit[0] <= RESULT_CACHE_SECONDS:
        return hit[1]
    if not _run_lock.acquire(blocking=False):
        raise BacktestBusy("Đang có một backtest khác chạy. Thử lại sau ít phút.")
    try:
        data = load_data(symbols, s, e, cfg, progress=(lambda d, t, x: progress("data", d, t, x)) if progress else None)
        report = run_backtest(data, s, e, cfg, progress=(lambda d, t, x: progress("sim", d, t, x)) if progress else None)
    finally:
        _run_lock.release()
    _result_cache[key] = (time.time(), report)
    return report


def is_running() -> bool:
    return _run_lock.locked()


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Backtest chiến lược FinTech Bot")
    parser.add_argument("--symbols", help="Danh sách mã, VD FPT,HPG (mặc định BACKTEST_SYMBOLS)")
    parser.add_argument("--start", help="BACKTEST_START, VD 2026-01-01")
    parser.add_argument("--end", help="BACKTEST_END, VD 2026-06-30")
    parser.add_argument("--period", choices=list(PERIOD_MONTHS), help="Thay cho --start: 1M, 3M, 6M, 1Y")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

    def show(stage, done, total, text):
        if stage == "data" or done == total or done % 20 == 0:
            print(f"[{'dữ liệu' if stage == 'data' else 'mô phỏng'} {done}/{total}] {text}", flush=True)

    symbols = [x.strip().upper() for x in args.symbols.split(",")] if args.symbols else None
    report = run(symbols, args.start, args.end, PERIOD_MONTHS.get(args.period) if args.period else None,
                 progress=show, use_cache=False)
    folder = save_report(report)
    print()
    print(summary_text(report))
    print(f"\nĐã lưu báo cáo tại: {folder}")
    print(f"Tổng lãi/lỗ ròng các giao dịch: {fmt_money(report['metrics']['net_pnl'])}")


if __name__ == "__main__":
    main()
