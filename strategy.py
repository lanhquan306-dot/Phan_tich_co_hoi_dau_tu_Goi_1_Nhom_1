"""Chiến lược chấm điểm rule-based (thuần tính toán: không gọi mạng, không gọi Telegram).

Tổng điểm = T1 (30) + T2 (30) + T3 (40).
- T1: EPS/LNST mẹ YoY (20) + ROE TTM (10)
- T2: Delta ROC63 so với VN-Index (15) + xu hướng MA50/MA150 (10) + gần đỉnh 52W (5)
- T3: breakout còn hiệu lực - BẮT BUỘC để BUY (25) + volume (10) + RSI (5)

Cổng thị trường (VN-Index):
- UPTREND : Close > MA50, phiên phân phối/25 < 4   -> BUY khi điểm >= 60, tỷ trọng chuẩn
- PRESSURE: Close > MA50, phiên phân phối/25 >= 4  -> BUY khi điểm >= 75, tỷ trọng giảm 1/2
- WEAK    : Close <= MA50 một phiên               -> không mở BUY mới
- DOWNTREND: Close < MA50 hai phiên liên tiếp      -> không mở BUY mới

Các hàm nhận `cfg` (mặc định config.STRATEGY) để selftests có thể truyền ngưỡng khác.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from config import BLOCKED_SYMBOLS, STRATEGY
from utils import fmt_price, to_float


def wilder_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """RSI theo phương pháp làm mượt của Wilder (giống TradingView/AmiBroker).

    Giá trị đầu tiên ở phiên thứ `period` là trung bình cộng lãi/lỗ; các phiên
    sau dùng trung bình trượt: avg = ((period - 1) * avg + giá_trị_mới) / period.
    """
    c = pd.Series(close, dtype=float)
    delta = c.diff()
    # Vòng lặp chạy trên mảng numpy (nhanh hơn .iloc nhiều lần) - backtest gọi hàm này hàng nghìn lần.
    gain = delta.clip(lower=0).fillna(0.0).to_numpy()
    loss = (-delta.clip(upper=0)).fillna(0.0).to_numpy()
    values = np.full(len(c), np.nan)
    if len(c) <= period:
        return pd.Series(values, index=c.index, dtype=float)
    avg_gain = gain[1:period + 1].mean()
    avg_loss = loss[1:period + 1].mean()
    values[period] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    for i in range(period + 1, len(c)):
        avg_gain = ((period - 1) * avg_gain + gain[i]) / period
        avg_loss = ((period - 1) * avg_loss + loss[i]) / period
        values[i] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return pd.Series(values, index=c.index, dtype=float)


def roc(close: pd.Series, n: int):
    """ROC(n) = (Close_t - Close_{t-n}) / Close_{t-n} * 100, đơn vị %."""
    if len(close) <= n:
        return None
    old = to_float(close.iloc[-n - 1])
    cur = to_float(close.iloc[-1])
    if old in (None, 0) or cur is None:
        return None
    return ((cur - old) / old) * 100.0


def relative_roc(stock_close: pd.Series, stock_times: pd.Series,
                 index_df: pd.DataFrame | None, n: int = 63) -> dict:
    """T2.1: Delta(ROC n) = ROC_n(cổ phiếu) - ROC_n(VN-Index), đơn vị điểm %.

    VN-Index được cắt tới đúng ngày nến cuối của cổ phiếu để hai ROC cùng mốc
    thời gian (tránh lệch khi một bên có nến hôm nay còn bên kia chưa có).
    """
    out = {"roc": None, "index_roc": None, "delta": None,
           "stock_date": None, "index_date": None, "aligned": False}
    c = pd.to_numeric(pd.Series(stock_close), errors="coerce").reset_index(drop=True)
    t = pd.to_datetime(pd.Series(stock_times), errors="coerce").reset_index(drop=True)
    ok = c.notna() & t.notna()
    c, t = c[ok].reset_index(drop=True), t[ok].reset_index(drop=True)
    out["roc"] = roc(c, n)
    if len(t):
        out["stock_date"] = t.iloc[-1].date().isoformat()
    if index_df is None or len(index_df) == 0 or not len(t):
        return out
    ix = index_df.copy()
    ix["time"] = pd.to_datetime(ix["time"], errors="coerce")
    ix["close"] = pd.to_numeric(ix["close"], errors="coerce")
    ix = ix.dropna(subset=["time", "close"]).sort_values("time")
    ix = ix[ix["time"].dt.normalize() <= t.iloc[-1].normalize()].reset_index(drop=True)
    if ix.empty:
        return out
    out["index_date"] = ix["time"].iloc[-1].date().isoformat()
    out["aligned"] = out["index_date"] == out["stock_date"]
    out["index_roc"] = roc(ix["close"], n)
    if out["roc"] is not None and out["index_roc"] is not None:
        out["delta"] = out["roc"] - out["index_roc"]
    return out


_NO_BREAKOUT = {"found": False, "date": None, "age": None, "pivot": None,
                "event_close": None, "current_above": False, "in_buy_zone": False}


def _latest_breakout(close: pd.Series, times: pd.Series, pivot_n: int, window: int, buy_zone: float):
    """Tìm phiên breakout gần nhất và "đóng băng" pivot tại phiên đó.

    above[i] = close[i] > max(close[i-pivot_n : i])   (pivot = đỉnh đóng cửa pivot_n phiên trước)
    Phiên breakout là phiên ĐẦU TIÊN của một chuỗi above liên tiếp: above[i] and not above[i-1].
    Nhờ vậy giá lập đỉnh mới mỗi ngày không làm "tuổi" breakout bị đếm lại từ 0.
    Chỉ xét breakout trong `window` phiên gần nhất.
    """
    if len(close) < pivot_n + 1:
        return dict(_NO_BREAKOUT)

    pivot_series = close.shift(1).rolling(pivot_n, min_periods=pivot_n).max()
    above = (close > pivot_series) & pivot_series.notna()
    event = above & ~above.shift(1, fill_value=False)

    start = max(0, len(close) - int(window))
    event_idx = [i for i in range(start, len(close)) if bool(event.iloc[i])]
    if not event_idx:
        return dict(_NO_BREAKOUT)

    i = event_idx[-1]
    p = to_float(pivot_series.iloc[i])
    cur = to_float(close.iloc[-1])
    age = len(close) - 1 - i
    zone = p * (1 + buy_zone) if p is not None else None
    current_above = bool(cur is not None and p is not None and cur > p)
    in_zone = bool(current_above and zone is not None and cur <= zone)
    t = pd.Timestamp(times.iloc[i]).date().isoformat() if times is not None else None
    return {
        "found": True,
        "date": t,
        "age": age,
        "pivot": p,
        "event_close": to_float(close.iloc[i]),
        "current_above": current_above,
        "buy_zone": zone,
        "in_buy_zone": in_zone,
    }


def compute_technical(df: pd.DataFrame, index_df: pd.DataFrame | None = None, cfg=None) -> dict:
    """Mọi chỉ báo kỹ thuật chiến lược cần, tính trên nến cuối của `df`."""
    cfg = cfg or STRATEGY
    if df is None or len(df) < 30:
        raise ValueError("Không đủ lịch sử để tính chỉ báo kỹ thuật.")

    x = df.copy().sort_values("time").reset_index(drop=True)
    c = pd.to_numeric(x["close"], errors="coerce")
    v = pd.to_numeric(x["volume"], errors="coerce")
    low = pd.to_numeric(x["low"], errors="coerce")

    ma20s = c.rolling(20).mean()
    ma50s = c.rolling(50).mean()
    ma150s = c.rolling(150).mean()
    rsi = wilder_rsi(c, 14)

    # Tỷ lệ khối lượng phiên cuối cho quy tắc thoát lệnh: V(t) / TB(V của 20 phiên trước đó).
    vol20_prev = to_float(v.iloc[-21:-1].mean()) if len(x) >= 21 else None
    vol_today = to_float(v.iloc[-1])
    vol_ratio = to_float(vol_today / vol20_prev) if vol_today is not None and vol20_prev not in (None, 0) else None

    # T3.2 dùng hai cửa sổ không chồng lấn.
    # numerator = t,t-1,t-2; denominator = t-3 ... t-22 (20 sessions).
    vol3_avg = to_float(v.iloc[-3:].mean()) if len(x) >= 3 else None
    vol20_pre3 = to_float(v.iloc[-23:-3].mean()) if len(x) >= 23 else None
    vol_ratio_3d = to_float(vol3_avg / vol20_pre3) if vol3_avg is not None and vol20_pre3 not in (None, 0) else None

    current_pivot = to_float(c.iloc[-cfg.pivot_n - 1:-1].max()) if len(x) >= cfg.pivot_n + 1 else None
    breakout = _latest_breakout(c, x["time"], cfg.pivot_n, cfg.breakout_window, cfg.buy_zone)

    low20 = to_float(low.iloc[-cfg.exit_low_n - 1:-1].min()) if len(x) >= cfg.exit_low_n + 1 else None
    high52 = to_float(c.iloc[-252:].max()) if len(x) >= 252 else None
    close_now = to_float(c.iloc[-1])
    high52_ratio = to_float(close_now / high52) if close_now is not None and high52 not in (None, 0) else None
    adv20 = to_float((c.iloc[-20:] * v.iloc[-20:]).mean()) if len(x) >= 20 else None

    ma50 = to_float(ma50s.iloc[-1]) if len(x) >= 50 else None
    ma150 = to_float(ma150s.iloc[-1]) if len(x) >= 150 else None
    ma50_10 = to_float(ma50s.iloc[-11]) if len(x) >= 60 else None
    prev_ma50 = to_float(ma50s.iloc[-2]) if len(x) >= 51 else None
    prev_close = to_float(c.iloc[-2]) if len(x) >= 2 else None
    below_ma50_2 = bool(
        close_now is not None and ma50 is not None and prev_close is not None and prev_ma50 is not None
        and close_now < ma50 and prev_close < prev_ma50
    )

    rel = relative_roc(c, x["time"], index_df, cfg.t21_roc_n)

    return {
        "history_count": len(x),
        "close": close_now,
        "ma20": to_float(ma20s.iloc[-1]) if len(x) >= 20 else None,
        "ma50": ma50,
        "ma150": ma150,
        "ma50_down": bool(ma50 is not None and ma50_10 is not None and ma50 < ma50_10),
        "below_ma50_2": below_ma50_2,
        "rsi14": to_float(rsi.iloc[-1]),
        "vol_ratio": vol_ratio,
        "vol3_avg": vol3_avg,
        "vol20_pre3": vol20_pre3,
        "vol_ratio_3d": vol_ratio_3d,
        "current_pivot": current_pivot,
        "pivot": breakout.get("pivot") if breakout.get("found") else current_pivot,
        "breakout": breakout,
        "low20": low20,
        "high52": high52,
        "high52_ratio": high52_ratio,
        "adv20": adv20,
        "roc63": rel["roc"],
        "index_roc63": rel["index_roc"],
        "delta_roc63": rel["delta"],
        "roc_stock_date": rel["stock_date"],
        "roc_index_date": rel["index_date"],
        "roc_aligned": rel["aligned"],
    }


def distribution_days(index_df: pd.DataFrame, cfg=None) -> dict:
    """Đếm phiên phân phối của VN-Index trong `dist_window` phiên gần nhất.

    Phiên phân phối (O'Neil): VN-Index giảm >= dist_drop_pct% so với phiên trước
    VÀ khối lượng lớn hơn phiên trước. Trả count=None nếu VN-Index không có volume.
    """
    cfg = cfg or STRATEGY
    n = int(cfg.dist_window)
    x = index_df.sort_values("time").reset_index(drop=True).iloc[-(n + 1):].copy()
    close = pd.to_numeric(x["close"], errors="coerce")
    vol = pd.to_numeric(x["volume"], errors="coerce")
    if vol.fillna(0).gt(0).sum() < min(20, n):
        return {"count": None, "dates": []}
    ret = close.pct_change()
    is_dist = (ret <= -cfg.dist_drop_pct / 100.0) & (vol > vol.shift(1))
    is_dist = is_dist.iloc[-n:].fillna(False)
    dates = [pd.Timestamp(t).date().isoformat() for t in x["time"].iloc[-n:][is_dist.values]]
    return {"count": int(is_dist.sum()), "dates": dates}


def market_gate(index_df: pd.DataFrame, cfg=None) -> dict:
    """Trạng thái thị trường + ngưỡng BUY + hệ số tỷ trọng theo bảng cổng thị trường."""
    cfg = cfg or STRATEGY
    ma_n, n, k = int(cfg.market_ma), int(cfg.dist_window), int(cfg.dist_threshold)
    base = {
        "state": "UNKNOWN", "close": None, "ma50": None, "distribution_days": None,
        "distribution_dates": [], "allow_buy": False, "buy_threshold": None,
        "weight_factor": 0.0, "reason": "Không đủ dữ liệu VN-Index.",
    }
    if index_df is None or len(index_df) < ma_n + 2:
        return base

    x = index_df.sort_values("time").reset_index(drop=True)
    c = pd.to_numeric(x["close"], errors="coerce")
    ma = c.rolling(ma_n).mean()
    cur, cur_ma = to_float(c.iloc[-1]), to_float(ma.iloc[-1])
    prev, prev_ma = to_float(c.iloc[-2]), to_float(ma.iloc[-2])
    dist = distribution_days(x, cfg)
    cnt = dist["count"]
    base.update(close=cur, ma50=cur_ma, distribution_days=cnt, distribution_dates=dist["dates"])

    if None in (cur, cur_ma):
        base["reason"] = "Không xác định được VN-Index / MA50."
        return base

    if prev is not None and prev_ma is not None and cur < cur_ma and prev < prev_ma:
        base.update(state="DOWNTREND",
                    reason=f"VN-Index đóng cửa dưới MA{ma_n} hai phiên liên tiếp: không mở BUY mới.")
    elif cur <= cur_ma:
        base.update(state="WEAK",
                    reason=f"VN-Index dưới/đúng MA{ma_n} một phiên: không mở BUY mới, chờ xác nhận.")
    elif cnt is None:
        # Thiếu volume chỉ số -> không kiểm được điều kiện UPTREND, dùng mức thận trọng.
        base.update(state="PRESSURE", allow_buy=True, buy_threshold=cfg.buy_score_pressure,
                    weight_factor=cfg.pressure_weight_factor,
                    reason=f"VN-Index trên MA{ma_n} nhưng thiếu volume để đếm phiên phân phối: áp dụng PRESSURE.")
    elif cnt >= k:
        base.update(state="PRESSURE", allow_buy=True, buy_threshold=cfg.buy_score_pressure,
                    weight_factor=cfg.pressure_weight_factor,
                    reason=f"VN-Index trên MA{ma_n} và có {cnt} phiên phân phối/{n} (>= {k}).")
    else:
        base.update(state="UPTREND", allow_buy=True, buy_threshold=cfg.buy_score_uptrend,
                    weight_factor=1.0,
                    reason=f"VN-Index trên MA{ma_n} và có {cnt} phiên phân phối/{n} (< {k}).")
    return base


def score_tier1(fund: dict) -> dict:
    """T1 (30đ): T1.1 EPS/LNST mẹ YoY >=20% -> 20, >=10% -> 8; T1.2 ROE TTM >=15% -> 10, >=10% -> 5."""
    eps = to_float(fund.get("eps_yoy"))
    eps_status = fund.get("eps_status")
    roe = to_float(fund.get("roe"))

    if eps_status == "ok" and eps is not None:
        eps_pts = 20 if eps >= 20 else 8 if eps >= 10 else 0
        eps_detail = f"EPS/LNST mẹ YoY {eps:+.1f}%"
    elif eps_status == "base_nonpositive":
        eps_pts = 0
        eps_detail = "EPS/LNST cùng kỳ <= 0: 0 điểm theo quy tắc"
    else:
        eps_pts = 0
        eps_detail = "Thiếu EPS/LNST YoY: 0 điểm"

    if roe is None:
        roe_pts = 0
        roe_detail = "Thiếu ROE TTM: 0 điểm"
    else:
        roe_pts = 10 if roe >= 15 else 5 if roe >= 10 else 0
        roe_detail = f"ROE TTM {roe:.1f}%"

    score = eps_pts + roe_pts
    return {
        "score": score, "max": 30,
        "components": {
            "T1.1": {"points": eps_pts, "max": 20, "value": eps, "detail": eps_detail},
            "T1.2": {"points": roe_pts, "max": 10, "value": roe, "detail": roe_detail},
        },
    }


def score_t21(tech: dict, cfg=None) -> dict:
    """T2.1 = Delta ROC63 = ROC63(mã) - ROC63(VN-Index). points=None nghĩa là UNKNOWN."""
    cfg = cfg or STRATEGY
    n = int(cfg.t21_roc_n)
    delta = to_float(tech.get("delta_roc63"))
    r_s, r_i = to_float(tech.get("roc63")), to_float(tech.get("index_roc63"))
    if delta is None:
        why = "thiếu ROC mã" if r_s is None else "thiếu ROC VN-Index"
        return {"points": None, "max": 15, "value": None, "detail": f"Delta ROC{n} N/A ({why})"}
    # Làm tròn để 25% - 10% không thành 14.999999 và rớt khỏi mốc 15.
    delta = round(delta, 6)
    pts = 15 if delta >= cfg.t21_delta_full else 8 if delta >= cfg.t21_delta_partial else 0
    detail = f"Delta ROC{n} = {r_s:+.1f}% − VNI {r_i:+.1f}% = {delta:+.1f} điểm %"
    if tech.get("roc_aligned") is False:
        detail += f" (VNI tới {tech.get('roc_index_date')})"
    return {"points": pts, "max": 15, "value": delta, "detail": detail}


def score_tier2(tech: dict, cfg=None) -> dict:
    """T2 (30đ): T2.1 Delta ROC63 (15) + T2.2 xu hướng MA (10) + T2.3 gần đỉnh 52 tuần (5).

    Thiếu T2.1 thì điểm là một KHOẢNG [score_min, score_max] chứ không coi là 0.
    """
    close, ma50, ma150 = to_float(tech.get("close")), to_float(tech.get("ma50")), to_float(tech.get("ma150"))
    h52r = to_float(tech.get("high52_ratio"))

    t21 = score_t21(tech, cfg)
    rs_pts = t21["points"]
    if close is None or ma50 is None:
        trend_pts = 0
        trend_detail = "Thiếu Close/MA50"
    elif close <= ma50:
        trend_pts = 0
        trend_detail = f"Close {fmt_price(close)} <= MA50 {fmt_price(ma50)}"
    elif ma150 is not None and ma50 > ma150:
        trend_pts = 10
        trend_detail = "Close > MA50 và MA50 > MA150"
    else:
        trend_pts = 5
        trend_detail = "Close > MA50 nhưng MA50 chưa > MA150"

    high_pts = 5 if h52r is not None and h52r >= 0.85 else 2 if h52r is not None and h52r >= 0.75 else 0
    known = trend_pts + high_pts
    score_min = known + (0 if rs_pts is None else rs_pts)
    score_max = known + (15 if rs_pts is None else rs_pts)
    return {
        "score": score_min if score_min == score_max else None,
        "score_min": score_min,
        "score_max": score_max,
        "score_complete": rs_pts is not None,
        "max": 30,
        "components": {
            "T2.1": {"points": rs_pts, "max": 15, "value": t21["value"], "detail": t21["detail"]},
            "T2.2": {"points": trend_pts, "max": 10, "value": None, "detail": trend_detail},
            "T2.3": {"points": high_pts, "max": 5, "value": h52r,
                     "detail": "52W ratio N/A" if h52r is None else f"Close/đỉnh 52W = {h52r:.1%}"},
        }
    }


def score_tier3(tech: dict, cfg=None) -> dict:
    """T3 (40đ): T3.1 breakout còn hiệu lực (25, BẮT BUỘC để BUY) + T3.2 khối lượng (10) + T3.3 RSI (5)."""
    cfg = cfg or STRATEGY
    b = tech.get("breakout") or {}
    age = b.get("age")
    mandatory = bool(b.get("found") and b.get("in_buy_zone") and age is not None and age < cfg.breakout_window)
    if mandatory:
        breakout_pts = 25 if age <= 2 else 15
        breakout_detail = (
            f"Breakout {age} phiên trước tại Pivot {fmt_price(b.get('pivot'))}; "
            f"Close hiện tại trong ({fmt_price(b.get('pivot'))}, {fmt_price(b.get('buy_zone'))}]"
        )
    elif b.get("found"):
        breakout_pts = 0
        if not b.get("current_above"):
            why = "Close đã rơi về dưới/đúng Pivot đóng băng"
        else:
            why = f"Close đã vượt vùng mua +{cfg.buy_zone:.0%}"
        breakout_detail = f"Breakout không còn hiệu lực: {why}"
    else:
        breakout_pts = 0
        breakout_detail = f"Không có breakout khởi phát trong {cfg.breakout_window} phiên gần nhất"

    vr3 = to_float(tech.get("vol_ratio_3d"))
    if vr3 is None or vr3 < cfg.vol_score_low:
        vol_pts = 0
    else:
        vol_pts = 10 if vr3 >= cfg.vol_score_full else 5
    vol_detail = "Volume3/Base20 N/A" if vr3 is None else f"Volume3 / Base20 = {vr3:.2f}x"

    rsi = to_float(tech.get("rsi14"))
    rsi_pts = 5 if rsi is not None and cfg.rsi_min <= rsi <= cfg.rsi_max else 0
    rsi_detail = "RSI14 N/A" if rsi is None else f"RSI14 = {rsi:.1f}"

    return {
        "score": breakout_pts + vol_pts + rsi_pts,
        "max": 40,
        "mandatory_breakout": mandatory,
        "buy_zone": b.get("buy_zone"),
        "breakout_pivot": b.get("pivot"),
        "components": {
            "T3.1": {"points": breakout_pts, "max": 25, "value": age, "detail": breakout_detail},
            "T3.2": {"points": vol_pts, "max": 10, "value": vr3, "detail": vol_detail},
            "T3.3": {"points": rsi_pts, "max": 5, "value": rsi, "detail": rsi_detail},
        }
    }


def hard_filter(symbol: str, tech: dict, fund: dict, meta: dict, cfg=None) -> dict:
    """Điều kiện bắt buộc: sàn HOSE/HNX, không bị chặn, đủ thanh khoản, đủ lịch sử, nợ vay/VCSH."""
    cfg = cfg or STRATEGY
    exchange = str(meta.get("exchange") or "UNKNOWN").upper()
    history = int(tech.get("history_count") or 0)
    adv20 = to_float(tech.get("adv20"))
    de = to_float(fund.get("debt_equity"))
    financial = bool(fund.get("is_financial"))

    allowed = symbol.upper() not in BLOCKED_SYMBOLS
    checks = {
        "exchange": {"ok": exchange in {"HOSE", "HNX"}, "detail": f"Sàn {exchange}"},
        "status": {"ok": allowed,
                   "detail": "không nằm trong BLOCKED_SYMBOLS" if allowed else "nằm trong BLOCKED_SYMBOLS"},
        "liquidity": {"ok": adv20 is not None and adv20 >= cfg.min_adv_value,
                      "detail": f"GTGD20={adv20/1e9:.1f} tỷ" if adv20 is not None else "thiếu GTGD20"},
        "history": {"ok": history >= cfg.min_history, "detail": f"{history} phiên (cần >= {cfg.min_history})"},
    }
    if financial:
        checks["debt"] = {"ok": True, "detail": "nhóm tài chính: miễn D/E"}
    elif de is None:
        checks["debt"] = {"ok": False, "detail": "không đọc được nợ vay/VCSH"}
    else:
        checks["debt"] = {"ok": de <= cfg.max_debt_equity,
                          "detail": f"nợ vay/VCSH={de:.2f} (<= {cfg.max_debt_equity:g})"}
    return {"pass": all(v["ok"] for v in checks.values()), "checks": checks}


def _momentum_breakdown(tech: dict, close, cfg) -> bool:
    """Close thủng đáy N phiên + khối lượng lớn + RSI yếu."""
    low = to_float(tech.get("low20"))
    return (low is not None and close is not None and close < low
            and to_float(tech.get("vol_ratio"), 0) >= cfg.brk_vol_mult
            and to_float(tech.get("rsi14"), 100) < cfg.brk_rsi)


def exit_reason(tech: dict, gate: dict, position: dict | None, cfg=None) -> str | None:
    """Lý do bán nếu đang có vị thế: cắt lỗ, trailing stop, breakdown, gãy MA50, thị trường xấu.

    Ghi chú: hàm cập nhật `position["peak_close"]` (giá đóng cửa cao nhất từ khi mua).
    """
    cfg = cfg or STRATEGY
    if not position:
        return None
    close = to_float(tech.get("close"))
    if close is None:
        return None
    entry = to_float(position.get("entry_price"))
    if entry is None or entry <= 0:
        return None
    peak = max(to_float(position.get("peak_close"), entry), close)
    position["peak_close"] = peak

    if close <= entry * (1 - cfg.stop):
        return f"Cắt lỗ: Close {fmt_price(close)} <= {1 - cfg.stop:.2f} x giá mua {fmt_price(entry)}"
    if peak >= entry * (1 + cfg.trail_trigger) and close <= peak * (1 - cfg.trail):
        return f"Trailing: đã lãi >= {cfg.trail_trigger:.0%}, Close giảm >= {cfg.trail:.0%} từ đỉnh {fmt_price(peak)}"
    if _momentum_breakdown(tech, close, cfg):
        return (f"Breakdown: Close < đáy {cfg.exit_low_n} phiên + volume >= {cfg.brk_vol_mult:g}x "
                f"+ RSI < {cfg.brk_rsi:g}")
    if tech.get("below_ma50_2") or (
        tech.get("ma50") is not None and close < tech["ma50"] and to_float(tech.get("vol_ratio"), 0) >= 1.5
    ):
        return "Gãy MA50: dưới MA50 hai phiên liên tiếp hoặc một phiên với volume >= 1.5x"
    if gate.get("state") == "DOWNTREND" and tech.get("ma20") is not None and close < tech["ma20"]:
        return "Thị trường DOWNTREND và giá đóng cửa dưới MA20"
    return None


def avoid_reason(tech: dict, cfg=None) -> str | None:
    """Lý do KHÔNG mua mới dù chưa có vị thế."""
    cfg = cfg or STRATEGY
    close, ma50 = to_float(tech.get("close")), to_float(tech.get("ma50"))
    if close is not None and ma50 is not None and close < ma50 and tech.get("ma50_down"):
        return "Close < MA50 và MA50 đang dốc xuống"
    if _momentum_breakdown(tech, close, cfg):
        return "Momentum breakdown được xác nhận"
    return None


def evaluate(symbol: str, tech: dict, fund: dict, gate: dict, meta: dict,
             position: dict | None = None, cfg=None) -> dict:
    """Ra quyết định BUY / HOLD / SELL.

    Thứ tự ưu tiên:
        1. Có vị thế và chạm điều kiện thoát  -> SELL
        2. Có vị thế                          -> HOLD (NẮM GIỮ)
        3. Chưa có vị thế nhưng xu hướng xấu  -> HOLD (TRÁNH MUA)
        4. Thỏa MỌI điều kiện BUY             -> BUY
        5. Còn lại                            -> HOLD (CHỜ / CHỜ DỮ LIỆU), kèm lý do
    """
    cfg = cfg or STRATEGY
    fund = fund or {}
    t1 = score_tier1(fund)
    t2 = score_tier2(tech, cfg)
    t3 = score_tier3(tech, cfg)

    total_min = int(t1["score"] + t2["score_min"] + t3["score"])
    total_max = int(t1["score"] + t2["score_max"] + t3["score"])
    score_complete = total_min == total_max

    hard = hard_filter(symbol, tech, fund, meta or {}, cfg)
    state = gate.get("state")
    threshold = gate.get("buy_threshold")          # 60 (UPTREND) / 75 (PRESSURE) / None
    weight_factor = float(gate.get("weight_factor") or 0.0)

    sell = exit_reason(tech, gate, position, cfg)
    avoid = avoid_reason(tech, cfg) if position is None else None
    reasons = []
    hold_type = None

    score_pass = threshold is not None and total_min >= threshold
    score_fail = threshold is not None and total_max < threshold
    score_uncertain = threshold is not None and total_min < threshold <= total_max

    buy_checks = {
        "market": bool(gate.get("allow_buy")),
        "hard_filter": hard["pass"],
        "fundamental_source": bool(fund.get("latest_period") or fund.get("available")),
        "mandatory_breakout": t3["mandatory_breakout"],
        # Khi T2.1 thiếu: chỉ BUY nếu ngay cả điểm tối thiểu đã đạt ngưỡng.
        "score": score_pass,
        "no_position": position is None,
    }

    if position and sell:
        signal = "SELL"
        reasons.append(sell)
    elif position:
        signal, hold_type = "HOLD", "NẮM GIỮ"
        reasons.append("Đang có vị thế và chưa chạm điều kiện thoát.")
    elif avoid:
        signal, hold_type = "HOLD", "TRÁNH MUA"
        reasons.append(avoid + ".")
    elif all(buy_checks.values()):
        signal = "BUY"
        reasons.append(f"Thị trường {state}; điểm {total_min}/100 >= ngưỡng {threshold:g}.")
        if state == "PRESSURE":
            reasons.append("PRESSURE: tỷ trọng khuyến nghị giảm một nửa.")
        reasons.append(t3["components"]["T3.1"]["detail"] + ".")
        reasons.append(
            f"Nếu mua phiên kế tiếp: chỉ vào khi Open > Pivot {fmt_price(t3['breakout_pivot'])} "
            f"và Open <= {fmt_price(t3['buy_zone'])}."
        )
    else:
        signal = "HOLD"
        others_ok = all(buy_checks[k] for k in ("market", "hard_filter", "fundamental_source",
                                                 "mandatory_breakout", "no_position"))
        hold_type = "CHỜ DỮ LIỆU" if score_uncertain and others_ok else "CHỜ"
        if not buy_checks["market"]:
            reasons.append(f"Cổng thị trường {state}: không mở BUY mới.")
        if not buy_checks["hard_filter"]:
            failed = [v["detail"] for v in hard["checks"].values() if not v["ok"]]
            reasons.append("Bộ lọc cứng chưa đạt: " + "; ".join(failed) + ".")
        if not buy_checks["fundamental_source"]:
            reasons.append("Không có BCTC khả dụng: không phát BUY.")
        if not buy_checks["mandatory_breakout"]:
            reasons.append("T3.1 = 0: " + t3["components"]["T3.1"]["detail"] + ".")
        if score_fail:
            reasons.append(f"Điểm tối đa có thể đạt {total_max}/100 < ngưỡng BUY {threshold:g} ({state}).")
        elif score_uncertain:
            reasons.append(f"T2.1 chưa có nên tổng điểm trong khoảng {total_min}–{total_max}/100; "
                           f"ngưỡng BUY {threshold:g} nằm trong khoảng này.")
        elif score_pass:
            reasons.append(f"Điểm {total_min}/100 đã đạt ngưỡng {threshold:g}, nhưng còn điều kiện khác chưa đạt.")

    close = to_float(tech.get("close"))
    entry = to_float((position or {}).get("entry_price"))
    stop_base = entry if entry is not None else close
    return {
        "signal": signal,
        "hold_type": hold_type,
        "score": total_min,
        "score_min": total_min,
        "score_max": total_max,
        "score_complete": score_complete,
        "threshold": threshold,
        "weight": cfg.position_weight * weight_factor if signal == "BUY" else None,
        "tier1": t1,
        "tier2": t2,
        "tier3": t3,
        "hard": hard,
        "checks": buy_checks,
        "reasons": reasons,
        "buy_zone": t3.get("buy_zone"),
        "breakout_pivot": t3.get("breakout_pivot"),
        "stop_ref": stop_base * (1 - cfg.stop) if stop_base is not None else None,
    }
