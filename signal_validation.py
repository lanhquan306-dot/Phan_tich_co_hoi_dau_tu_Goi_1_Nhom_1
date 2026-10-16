"""Kiểm định tín hiệu quá khứ ("test ở quá khứ") cho các phương thức BUY / HOLD / SELL của bot.

Khác với backtest danh mục (backtest.py) — vốn trả lời "nếu cầm 100 triệu thì lãi bao nhiêu" và bị giới
hạn bởi vốn, 5 vị thế, lô 100 — module này trả lời câu hỏi: **các quy tắc tính BUY/HOLD/SELL có đúng
không khi áp vào quá khứ?**

Cách làm (không đổi một dòng nào của strategy.py):
    1. REPLAY: với mỗi mã, mỗi phiên t trong quá khứ, dựng lại đúng pipeline của bot live
       (backtest.Replay: dữ liệu <= t, VN-Index <= t, BCTC đã công bố tại t) -> evaluate() ra tín hiệu.
       Mỗi mã có một "vị thế ảo" đi theo đúng khuyến nghị: BUY -> mua ở Open t+1 (nếu Open trong vùng
       mua, không ở giá trần), sau đó bot xét SELL mỗi ngày; SELL -> bán ở Open phiên kế tiếp (đủ T+2).
       Nhờ vậy cả 6 loại kết luận đều xuất hiện: BUY, SELL, HOLD-NẮM GIỮ, HOLD-CHỜ, HOLD-CHỜ DỮ LIỆU,
       HOLD-TRÁNH MUA.
    2. CHẤM ĐÚNG/SAI (chỉ SAU KHI replay xong, dữ liệu tương lai không bao giờ đi vào bước 1):
       lợi nhuận 5/10/20 phiên sau tín hiệu (close t -> close t+H) và so với VN-Index cùng khoảng.
         BUY       đúng nếu giá 20 phiên sau cao hơn
         SELL      đúng nếu giá 20 phiên sau thấp hơn (bán tránh được lỗ)
         NẮM GIỮ   đúng nếu giá 20 phiên sau không thấp hơn
         TRÁNH MUA đúng nếu giá 20 phiên sau không cao hơn
         CHỜ       không có đúng/sai; đo tỷ lệ "bỏ lỡ" (sau đó tăng > 10%)
    3. KIỂM TRA PHƯƠNG PHÁP:
       - Điểm số (0–100) có dự báo được lợi nhuận không: nhóm điểm, hệ số IC (tương quan hạng Spearman),
         IC riêng từng tầng T1/T2/T3.
       - Cổng thị trường: VN-Index 20 phiên sau theo trạng thái UPTREND/PRESSURE/WEAK/DOWNTREND.
       - So với "mốc ngẫu nhiên" = trung bình mọi (mã, phiên): tín hiệu chỉ có giá trị nếu tốt hơn mốc này.

Lưu ý thống kê: các cửa sổ 20 phiên chồng lên nhau nên số quan sát "hiệu dụng" nhỏ hơn số dòng;
module dùng n/H khi ước lượng độ tin cậy. Kết quả là in-sample, không phải bằng chứng cho tương lai.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import math
import time
from collections import Counter
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

import backtest as bt
from config import BACKTEST, STRATEGY, VALIDATION, BacktestConfig
from utils import to_float

log = logging.getLogger("fintech-bot.validation")

HORIZONS = (5, 10, 20)
MAIN_H = 20
MISSED_GAIN = VALIDATION.missed_gain   # CHỜ mà sau đó tăng > mức này -> "bỏ lỡ"
MIN_SAMPLE = 20             # dưới mức này: "chưa đủ mẫu"
PERIOD_MONTHS = {"3M": 3, "6M": 6, "1Y": 12, "2Y": 24}
LABELS = ("BUY", "SELL", "NẮM GIỮ", "CHỜ", "CHỜ DỮ LIỆU", "TRÁNH MUA")
SCORE_BINS = [(0, 19), (20, 39), (40, 59), (60, 79), (80, 100)]
GATES = ("UPTREND", "PRESSURE", "WEAK", "DOWNTREND")


def label_of(r: dict) -> str:
    return r["signal"] if r["signal"] in ("BUY", "SELL") else (r.get("hold_type") or "CHỜ")


# =====================================================================
# 1. Replay: dựng lại tín hiệu từng phiên với vị thế ảo
# =====================================================================
def replay_signals(data: bt.BacktestData, start: date, end: date, cfg: BacktestConfig = BACKTEST,
                   strat_cfg=STRATEGY, progress=None) -> tuple[pd.DataFrame, list[dict], list[str]]:
    """Trả về (bảng tín hiệu mỗi mã × phiên, danh sách giao dịch ảo, cảnh báo)."""
    replay = bt.Replay(data, cfg, strat_cfg)
    calendar = replay.calendar(start, end)
    if len(calendar) < 2:
        raise ValueError("Khoảng kiểm định có ít hơn 2 phiên giao dịch.")
    rows, trades, warnings = [], [], []
    total = len(replay.symbols) * len(calendar)
    step = 0
    for s in replay.symbols:
        frame, dpos = replay.frames[s], replay.day_pos[s]
        exch = data.meta[s]["exchange"]
        pos = None                 # vị thế ảo đang giữ
        pending = None             # ("BUY", info) | ("SELL", info) chờ khớp phiên sau
        errors = 0
        for k, day in enumerate(calendar):
            step += 1
            if progress and step % 25 == 0:
                progress(step, total, f"{s} {day:%d/%m/%Y}")
            i = dpos.get(day)
            # ---- khớp lệnh ảo ở Open phiên này (tín hiệu của phiên trước)
            if pending and i is not None:
                kind, info = pending
                o = to_float(frame["open"].iloc[i], to_float(frame["close"].iloc[i]))
                if kind == "BUY":
                    pending = None
                    if bt.at_ceiling(o, info["signal_close"], exch):
                        info["skip"] = "Open ở giá trần"
                    elif info["pivot"] is not None and o <= info["pivot"]:
                        info["skip"] = "Open ≤ Pivot"
                    elif info["buy_zone"] is not None and o > info["buy_zone"]:
                        info["skip"] = "Open vượt vùng mua"
                    else:
                        pos = {"entry_price": o, "peak_close": o, "entry_date": day, "entry_k": k,
                               "signal_date": info["date"]}
                elif kind == "SELL" and pos and k - pos["entry_k"] >= cfg.settlement_days:
                    pending = None
                    trades.append({"symbol": s, "buy_signal": pos["signal_date"], "entry_date": pos["entry_date"],
                                   "entry_price": pos["entry_price"], "sell_signal": info["date"], "exit_date": day,
                                   "exit_price": o, "return_pct": (o / pos["entry_price"] - 1) * 100,
                                   "net_pct": (o * (1 - cfg.transaction_fee - cfg.sell_tax)
                                               / (pos["entry_price"] * (1 + cfg.transaction_fee)) - 1) * 100,
                                   "sessions": k - pos["entry_k"], "reason": info["reason"], "open": False})
                    pos = None
            if i is None:
                continue
            gate, _ = replay.gate(day)
            try:
                tech, fund, r = replay.evaluate(s, day, pos)
            except Exception:
                errors += 1
                continue
            lab = label_of(r)
            rows.append({
                "date": day, "symbol": s, "market": gate.get("state"), "signal": r["signal"], "label": lab,
                "score": r.get("score_min"), "score_max": r.get("score_max"), "threshold": r.get("threshold"),
                "t1": (r.get("tier1") or {}).get("score"), "t2": (r.get("tier2") or {}).get("score_min"),
                "t3": (r.get("tier3") or {}).get("score"), "close": tech.get("close"), "in_position": pos is not None,
                **{f"ok_{c}": bool((r.get("checks") or {}).get(c)) for c in bt.CHECK_KEYS},
                "reason": " | ".join(r.get("reasons") or []),
            })
            if k == len(calendar) - 1:
                # "Hôm nay" nhìn từ người chưa có vị thế (giống /signal không có /position) để đối chiếu.
                flat = lab
                if pos is not None:
                    try:
                        flat = label_of(replay.evaluate(s, day, None)[2])
                    except Exception:
                        pass
                rows[-1]["label_flat"] = flat
                continue                                   # phiên cuối: không còn t+1
            if pending is not None:
                continue                                   # đang chờ khớp lệnh bán (chưa đủ T+2)
            if lab == "BUY" and pos is None:
                pending = ("BUY", {"date": day, "signal_close": tech.get("close"),
                                   "pivot": r.get("breakout_pivot"), "buy_zone": r.get("buy_zone")})
            elif lab == "SELL" and pos is not None:
                pending = ("SELL", {"date": day, "reason": "; ".join(r.get("reasons") or [])})
        if pos is not None:                                # còn giữ tới cuối kỳ: định giá theo close cuối
            last = to_float(frame.loc[frame["time"] <= calendar[-1], "close"].iloc[-1])
            trades.append({"symbol": s, "buy_signal": pos["signal_date"], "entry_date": pos["entry_date"],
                           "entry_price": pos["entry_price"], "sell_signal": None, "exit_date": calendar[-1],
                           "exit_price": last, "return_pct": (last / pos["entry_price"] - 1) * 100,
                           "net_pct": (last * (1 - cfg.transaction_fee - cfg.sell_tax)
                                       / (pos["entry_price"] * (1 + cfg.transaction_fee)) - 1) * 100,
                           "sessions": len(calendar) - 1 - pos["entry_k"], "reason": "Còn giữ tới cuối kỳ",
                           "open": True})
        if errors:
            warnings.append(f"{s}: {errors} phiên không tính được chỉ báo.")
    return pd.DataFrame(rows), trades, warnings


# =====================================================================
# 2. Lợi nhuận sau tín hiệu (chỉ dùng để CHẤM, sau khi replay xong)
# =====================================================================
def attach_forward_returns(signals: pd.DataFrame, data: bt.BacktestData) -> pd.DataFrame:
    """Thêm fwd{H} (% giá mã) và vni{H} (% VN-Index) từ close t tới close t+H phiên."""
    if signals.empty:
        return signals
    out = []
    idx = data.index.sort_values("time").reset_index(drop=True)
    it = pd.to_datetime(idx["time"]).dt.normalize()
    ic = pd.to_numeric(idx["close"], errors="coerce").to_numpy()
    ipos = {t: i for i, t in enumerate(it)}
    for s, g in signals.groupby("symbol", sort=False):
        f = data.prices[s].sort_values("time").reset_index(drop=True)
        ft = pd.to_datetime(f["time"]).dt.normalize()
        fc = pd.to_numeric(f["close"], errors="coerce").to_numpy()
        fpos = {t: i for i, t in enumerate(ft)}
        g = g.copy()
        for h in HORIZONS:
            vals, vvals = [], []
            for d in g["date"]:
                i, j = fpos.get(d), ipos.get(d)
                vals.append((fc[i + h] / fc[i] - 1) * 100 if i is not None and i + h < len(fc) and fc[i] else np.nan)
                vvals.append((ic[j + h] / ic[j] - 1) * 100 if j is not None and j + h < len(ic) and ic[j] else np.nan)
            g[f"fwd{h}"] = vals
            g[f"vni{h}"] = vvals
            g[f"ex{h}"] = g[f"fwd{h}"] - g[f"vni{h}"]
        out.append(g)
    res = pd.concat(out).sort_values(["date", "symbol"]).reset_index(drop=True)
    day_no = {d: i for i, d in enumerate(sorted(res["date"].unique()))}
    res["blk"] = [day_no[d] // MAIN_H for d in res["date"]]
    res["correct"] = [_correct(lab, fwd) for lab, fwd in zip(res["label"], res[f"fwd{MAIN_H}"])]
    return res


def _correct(label: str, fwd) -> float:
    """1 = đúng, 0 = sai, NaN = không chấm (CHỜ hoặc chưa đủ dữ liệu tương lai)."""
    if fwd is None or (isinstance(fwd, float) and math.isnan(fwd)):
        return np.nan
    if label == "BUY":
        return float(fwd > 0)
    if label == "SELL":
        return float(fwd < 0)
    if label == "NẮM GIỮ":
        return float(fwd >= 0)
    if label == "TRÁNH MUA":
        return float(fwd <= 0)
    return np.nan


# =====================================================================
# 3. Thống kê kiểm định
# =====================================================================
def spearman(x, y) -> float | None:
    """Tương quan hạng Spearman (không cần scipy). None nếu < 10 cặp hoặc một biến không đổi."""
    d = pd.DataFrame({"x": x, "y": y}).dropna()
    if len(d) < 10 or d["x"].nunique() < 2 or d["y"].nunique() < 2:
        return None
    return float(d["x"].rank().corr(d["y"].rank()))


def _n_eff(df: pd.DataFrame) -> int:
    """Số quan sát hiệu dụng: các cửa sổ 20 phiên chồng nhau của CÙNG một mã chỉ tính là một
    (đếm số cặp (mã, khối 20 phiên) khác nhau)."""
    return int(df[["symbol", "blk"]].drop_duplicates().shape[0]) if len(df) else 0


def _tstat(a: pd.DataFrame, b: pd.DataFrame, col: str = f"fwd{MAIN_H}") -> float | None:
    """t-stat chênh lệch trung bình giữa hai nhóm, dùng số quan sát hiệu dụng thay cho số dòng."""
    a, b = a.dropna(subset=[col]), b.dropna(subset=[col])
    na, nb = _n_eff(a), _n_eff(b)
    if na < 3 or nb < 3 or len(a) < 2 or len(b) < 2:
        return None
    se = math.sqrt(a[col].var(ddof=1) / na + b[col].var(ddof=1) / nb)
    return None if se == 0 or math.isnan(se) else float((a[col].mean() - b[col].mean()) / se)


def confidence(t) -> str:
    if t is None:
        return "chưa đủ mẫu"
    a = abs(t)
    return "cao" if a >= 2 else "trung bình" if a >= 1 else "thấp"


def _group_stats(g: pd.DataFrame) -> dict:
    ev = g.dropna(subset=[f"fwd{MAIN_H}"])
    return {
        "n": len(g), "n_eval": len(ev),
        **{f"mean{h}": float(g[f"fwd{h}"].mean()) if g[f"fwd{h}"].notna().any() else None for h in HORIZONS},
        "median": float(ev[f"fwd{MAIN_H}"].median()) if len(ev) else None,
        "excess": float(ev[f"ex{MAIN_H}"].mean()) if len(ev) and ev[f"ex{MAIN_H}"].notna().any() else None,
        "up_rate": float((ev[f"fwd{MAIN_H}"] > 0).mean() * 100) if len(ev) else None,
        "hit": float(ev["correct"].mean() * 100) if ev["correct"].notna().any() else None,
    }


def evaluate_signals(sig: pd.DataFrame, trades: list[dict]) -> dict:
    """Mọi thống kê của báo cáo kiểm định."""
    base = _group_stats(sig)
    by_label = {lab: _group_stats(sig[sig["label"] == lab]) for lab in LABELS if (sig["label"] == lab).any()}
    ev = sig.dropna(subset=[f"fwd{MAIN_H}"])
    for lab, st in by_label.items():
        st["t"] = _tstat(ev[ev["label"] == lab], ev)
        st["n_eff"] = _n_eff(ev[ev["label"] == lab])
    base["t"] = None
    wait = ev[ev["label"].isin(["CHỜ", "CHỜ DỮ LIỆU"])]
    missed = float((wait[f"fwd{MAIN_H}"] > MISSED_GAIN).mean() * 100) if len(wait) else None

    # Điểm chỉ có ý nghĩa khi chưa giữ mã (khi đang giữ, bot xét SELL chứ không xét điểm để mua).
    flat_all = sig[~sig["in_position"]]
    buckets = []
    for lo, hi in SCORE_BINS:
        g = flat_all[(flat_all["score"] >= lo) & (flat_all["score"] <= hi)]
        buckets.append({"range": f"{lo}-{hi}", **_group_stats(g)})
    flat = ev[~ev["in_position"]]
    ic = {"score": spearman(flat["score"], flat[f"fwd{MAIN_H}"]),
          "score_excess": spearman(flat["score"], flat[f"ex{MAIN_H}"]),
          "t1": spearman(flat["t1"], flat[f"fwd{MAIN_H}"]), "t2": spearman(flat["t2"], flat[f"fwd{MAIN_H}"]),
          "t3": spearman(flat["t3"], flat[f"fwd{MAIN_H}"]), "n_eff": _n_eff(flat)}

    days = sig.drop_duplicates("date")[["date", "market", "blk", f"vni{MAIN_H}"]].assign(symbol="VNINDEX")
    up_days = days[days["market"] == "UPTREND"].dropna(subset=[f"vni{MAIN_H}"])
    down_days = days[days["market"].isin(["WEAK", "DOWNTREND"])].dropna(subset=[f"vni{MAIN_H}"])
    gate_t = _tstat(up_days, down_days, col=f"vni{MAIN_H}")
    gate_blocks = (_n_eff(up_days), _n_eff(down_days))
    gates = {}
    for gname in GATES:
        g = days[days["market"] == gname]
        v = g[f"vni{MAIN_H}"].dropna()
        if len(g):
            gates[gname] = {"days": len(g), "n_eval": len(v), "mean": float(v.mean()) if len(v) else None,
                            "up_rate": float((v > 0).mean() * 100) if len(v) else None}

    # Giao dịch ảo theo đúng khuyến nghị BUY -> SELL
    closed = [t for t in trades if not t["open"]]
    keys = {(t["symbol"], t["sell_signal"]) for t in closed}
    sold = ev[[(sym, d) in keys for sym, d in zip(ev["symbol"], ev["date"])]]
    after_sell = sold[f"fwd{MAIN_H}"].tolist()
    trade_stats = {
        "n": len(trades), "closed": len(closed), "open": len(trades) - len(closed),
        "win_rate": float(np.mean([t["net_pct"] > 0 for t in trades]) * 100) if trades else None,
        "avg_net": float(np.mean([t["net_pct"] for t in trades])) if trades else None,
        "avg_sessions": float(np.mean([t["sessions"] for t in trades])) if trades else None,
        "best": max(trades, key=lambda t: t["net_pct"]) if trades else None,
        "worst": min(trades, key=lambda t: t["net_pct"]) if trades else None,
        "after_sell_mean": float(np.mean(after_sell)) if after_sell else None,
        "after_sell_down": float(np.mean([x < 0 for x in after_sell]) * 100) if after_sell else None,
        "after_sell_n": len(after_sell),
        "after_sell_t": _tstat(sold, ev) if after_sell else None,
    }
    return {"base": base, "by_label": by_label, "missed_rate": missed, "buckets": buckets, "ic": ic,
            "gates": gates, "gate_t": gate_t, "gate_blocks": gate_blocks, "trades": trade_stats, "n_rows": len(sig), "n_eval": len(ev)}


def _judge(good: bool, t) -> str:
    """Mức kết luận theo độ lớn |t| (số quan sát hiệu dụng):
    ✅/❌ khi |t| ≥ 2 (khó do ngẫu nhiên) · 🟡 khi 1 ≤ |t| < 2 (có dấu hiệu, chưa chắc) · ⚪ khi |t| < 1 hoặc thiếu mẫu."""
    if t is None or abs(t) < 1:
        return "⚪"
    if abs(t) < 2:
        return "🟡"
    return "✅" if good else "❌"


def _phrase(icon: str, good: bool, good_text: str, bad_text: str, neutral: str) -> str:
    if icon == "⚪":
        return neutral
    text = good_text if good else bad_text
    return text if icon in ("✅", "❌") else f"có dấu hiệu {text} nhưng chưa chắc chắn"


def ic_zscore(ic, n_eff: int):
    """z ≈ IC·√(n−1): IC có ý nghĩa khi |z| ≥ 2."""
    return None if ic is None or n_eff < 5 else ic * math.sqrt(n_eff - 1)


def verdicts(st: dict) -> list[tuple[str, str]]:
    """Kết luận tự động dạng (biểu tượng, câu). Mẫu nhỏ hoặc khác biệt mờ -> không kết luận vội."""
    out = []
    bmean = st["base"].get(f"mean{MAIN_H}")
    lab = st["by_label"]
    neutral = "chưa khác biệt rõ so với mốc (có thể chỉ do ngẫu nhiên)"

    buy = lab.get("BUY")
    if not buy or buy["n_eval"] < MIN_SAMPLE:
        out.append(("⚪", f"BUY: chỉ {0 if not buy else buy['n_eval']} lần chấm được — chưa đủ mẫu "
                          f"(cần ≥ {MIN_SAMPLE}). Kiểm định nhiều mã hơn hoặc giai đoạn dài hơn."))
    else:
        good = buy[f"mean{MAIN_H}"] > bmean
        icon = _judge(good, buy["t"])
        out.append((icon, f"BUY: 20 phiên sau TB {buy[f'mean{MAIN_H}']:+.2f}%, tăng {buy['up_rate']:.0f}% số lần "
                          f"(mốc {bmean:+.2f}%, {st['base']['up_rate']:.0f}%) → "
                          + _phrase(icon, good, "tốt hơn chọn ngẫu nhiên", "KÉM hơn chọn ngẫu nhiên", neutral) + "."))

    avoid = lab.get("TRÁNH MUA")
    if avoid and avoid["n_eval"] >= MIN_SAMPLE:
        good = avoid[f"mean{MAIN_H}"] < bmean
        icon = _judge(good, avoid["t"])
        out.append((icon, f"TRÁNH MUA: 20 phiên sau TB {avoid[f'mean{MAIN_H}']:+.2f}% (mốc {bmean:+.2f}%) → "
                          + _phrase(icon, good, "loại đúng các mã yếu", "loại nhầm các mã tăng tốt", neutral) + "."))

    ts = st["trades"]
    if ts["after_sell_n"] >= 5:
        good = ts["after_sell_mean"] < bmean
        icon = _judge(good, ts["after_sell_t"])
        out.append((icon, f"SELL: sau khi bán, 20 phiên sau giá TB {ts['after_sell_mean']:+.2f}% "
                          f"({ts['after_sell_down']:.0f}% lần giảm tiếp; mốc {bmean:+.2f}%) → quy tắc thoát "
                          + _phrase(icon, good, "giúp tránh lỗ", "thường bán quá sớm", neutral) + "."))
    elif ts["closed"]:
        out.append(("⚪", f"SELL: mới có {ts['after_sell_n']} lần bán chấm được — chưa đủ mẫu."))
    else:
        out.append(("⚪", "SELL: không có lệnh bán nào (không có BUY hoặc chưa tới điều kiện thoát)."))

    ic = st["ic"]["score"]
    z = ic_zscore(ic, st["ic"].get("n_eff", 0))
    if ic is None:
        out.append(("⚪", "Điểm 0–100: chưa đủ dữ liệu để đo tương quan."))
    else:
        icon = _judge(ic > 0, z)
        ztxt = "chưa đủ mẫu" if z is None else f"z = {z:+.1f}"
        out.append((icon, f"Điểm 0–100: IC = {ic:+.3f} ({ztxt}) → "
                          + _phrase(icon, ic > 0, "điểm cao thì 20 phiên sau tăng tốt hơn",
                                    "điểm cao lại tăng KÉM hơn (ngược chiều)",
                                    "chưa dự báo được lợi nhuận 20 phiên") + "."))

    g = st["gates"]
    weak = [g[k] for k in ("WEAK", "DOWNTREND") if k in g and g[k]["mean"] is not None]
    if "UPTREND" in g and g["UPTREND"]["mean"] is not None and weak:
        wmean = sum(x["mean"] * x["n_eval"] for x in weak) / max(1, sum(x["n_eval"] for x in weak))
        good = g["UPTREND"]["mean"] > wmean
        icon = _judge(good, st.get("gate_t"))
        blocks = st.get("gate_blocks") or (0, 0)
        few = min(blocks) < 10       # chỉ MỘT chỉ số: cần đủ nhiều giai đoạn 20 phiên độc lập mới kết luận được
        if few and icon in ("✅", "❌"):
            icon = "🟡"
        out.append((icon, f"Cổng thị trường: VN-Index 20 phiên sau khi UPTREND {g['UPTREND']['mean']:+.2f}% vs "
                          f"WEAK/DOWNTREND {wmean:+.2f}% → phân loại "
                          + _phrase(icon, good, "đúng hướng", "NGƯỢC hướng", "chưa khác biệt rõ")
                          + (f" (mới có {blocks[0]}/{blocks[1]} giai đoạn 20 phiên độc lập; nên kiểm định ≥ 2 năm)"
                             if few else "") + "."))
    return out


# =====================================================================
# 3b. ĐỘ CHÍNH XÁC: mỗi (mã, phiên) là một "case" có nhãn thật từ giá tương lai
# =====================================================================
SCORED_LABELS = ("BUY", "SELL", "NẮM GIỮ", "TRÁNH MUA")      # các kết luận có đúng/sai
WAIT_LABELS = ("CHỜ", "CHỜ DỮ LIỆU")                          # chỉ đo "bỏ lỡ"
RULES = {
    "BUY": "giá tăng",
    "SELL": "giá giảm tiếp",
    "NẮM GIỮ": "giá không giảm",
    "TRÁNH MUA": "giá không tăng",
    "CHỜ": "không chấm, chỉ đo bỏ lỡ",
}


def actual_move(fwd, band: float) -> str | None:
    """Nhãn THẬT từ giá tương lai: TĂNG / GIẢM / ĐI NGANG (|thay đổi| ≤ band). None = chưa có tương lai."""
    x = to_float(fwd)
    if x is None:
        return None
    if x > band:
        return "TĂNG"
    if x < -band:
        return "GIẢM"
    return "ĐI NGANG"


def judge_case(label: str, actual: str | None, fwd, missed_gain: float) -> str:
    """So kết luận của bot với nhãn thật -> ĐÚNG / SAI / BỎ LỠ / KHÔNG BỎ LỠ / CHƯA CHẤM."""
    if actual is None:
        return "CHƯA CHẤM"
    if label in WAIT_LABELS:
        return "BỎ LỠ" if to_float(fwd, 0) > missed_gain else "KHÔNG BỎ LỠ"
    ok = {"BUY": actual == "TĂNG", "SELL": actual == "GIẢM",
          "NẮM GIỮ": actual != "GIẢM", "TRÁNH MUA": actual != "TĂNG"}.get(label)
    return "CHƯA CHẤM" if ok is None else ("ĐÚNG" if ok else "SAI")


def case_comment(label: str, result: str, fwd, h: int) -> str:
    """Nhận xét một dòng cho từng case."""
    if result == "CHƯA CHẤM":
        return f"Chưa đủ {h} phiên tương lai để chấm."
    x = to_float(fwd, 0.0)
    move = f"{h} phiên sau giá {x:+.1f}%"
    text = {
        ("BUY", "ĐÚNG"): "mua đúng, giá tăng",
        ("BUY", "SAI"): "mua sai, giá không tăng",
        ("SELL", "ĐÚNG"): "bán đúng, tránh được đoạn giảm",
        ("SELL", "SAI"): "bán hụt, giá không giảm tiếp",
        ("NẮM GIỮ", "ĐÚNG"): "giữ đúng, giá không giảm",
        ("NẮM GIỮ", "SAI"): "giữ sai, giá giảm",
        ("TRÁNH MUA", "ĐÚNG"): "tránh đúng, giá không tăng",
        ("TRÁNH MUA", "SAI"): "tránh nhầm, giá lại tăng",
    }.get((label, result))
    if text is None:
        text = "bỏ lỡ một nhịp tăng mạnh" if result == "BỎ LỠ" else "chờ hợp lý, không bỏ lỡ nhịp tăng lớn"
    return f"{move} → {text}."


def score_cases(sig: pd.DataFrame, h: int, band: float, missed_gain: float) -> pd.DataFrame:
    """Gắn nhãn thật + đúng/sai + nhận xét cho mọi case ở tầm nhìn h phiên."""
    if h not in HORIZONS:
        raise ValueError(f"Chỉ hỗ trợ tầm nhìn {HORIZONS} phiên.")
    c = sig.copy()
    c["label"] = c["label"].replace({"CHỜ DỮ LIỆU": "CHỜ DỮ LIỆU"})
    c["fwd"] = c[f"fwd{h}"]
    c["actual"] = [actual_move(x, band) for x in c["fwd"]]
    c["result"] = [judge_case(lab, act, x, missed_gain) for lab, act, x in zip(c["label"], c["actual"], c["fwd"])]
    c["comment"] = [case_comment(lab, res, x, h) for lab, res, x in zip(c["label"], c["result"], c["fwd"])]
    day_no = {d: i for i, d in enumerate(sorted(c["date"].unique()))}
    c["hblk"] = [day_no[d] // h for d in c["date"]]
    return c


def _baseline_prob(scored: pd.DataFrame) -> dict[str, dict[str, float]]:
    """Xác suất tự nhiên TĂNG / GIẢM / ĐI NGANG của từng mã (tính trên MỌI phiên đã chấm của mã đó)."""
    out = {}
    for s, g in scored.groupby("symbol"):
        n = len(g)
        out[s] = {k: float((g["actual"] == k).sum() / n) for k in ("TĂNG", "GIẢM", "ĐI NGANG")}
    return out


def _guess_prob(label: str, p: dict[str, float]) -> float:
    """Xác suất 'đoán bừa' đúng nếu nói `label` mà không nhìn gì cả."""
    return {"BUY": p["TĂNG"], "SELL": p["GIẢM"], "NẮM GIỮ": 1 - p["GIẢM"],
            "TRÁNH MUA": 1 - p["TĂNG"]}.get(label, float("nan"))


def _acc_block(g: pd.DataFrame, base: dict) -> dict:
    """Số case, số đúng, tỷ lệ đúng, tỷ lệ đoán bừa, z (đã hiệu chỉnh cửa sổ chồng nhau) và mức tin cậy."""
    g = g[g["result"].isin(["ĐÚNG", "SAI"])]
    n = len(g)
    if not n:
        return {"n": 0, "correct": 0, "acc": None, "guess": None, "diff": None, "z": None, "icon": "⚪", "n_eff": 0}
    correct = int((g["result"] == "ĐÚNG").sum())
    probs = np.array([_guess_prob(lab, base[s]) for lab, s in zip(g["label"], g["symbol"])])
    expected = float(probs.sum())
    var = float((probs * (1 - probs)).sum())
    n_eff = int(g[["symbol", "hblk"]].drop_duplicates().shape[0])
    z = None
    if var > 0 and n_eff >= 3:
        z = (correct - expected) / math.sqrt(var) * math.sqrt(n_eff / n)
    acc, guess = correct / n * 100, expected / n * 100
    return {"n": n, "correct": correct, "acc": acc, "guess": guess, "diff": acc - guess, "z": z,
            "icon": _judge(acc > guess, z), "n_eff": n_eff}


def accuracy_summary(cases: pd.DataFrame, h: int, trades: list[dict]) -> dict:
    """Mọi con số của báo cáo độ chính xác ở tầm nhìn h phiên."""
    scored = cases[cases["actual"].notna()]
    base = _baseline_prob(scored) if len(scored) else {}
    pooled = {k: float((scored["actual"] == k).mean()) for k in ("TĂNG", "GIẢM", "ĐI NGANG")} if len(scored) else {}
    main = scored[scored["label"].isin(SCORED_LABELS)]
    overall = _acc_block(main, base)
    by_label = {}
    for lab in (*SCORED_LABELS, *WAIT_LABELS):
        g = cases[cases["label"] == lab]
        if not len(g):
            continue
        gs = g[g["actual"].notna()]
        if lab in WAIT_LABELS:
            missed = int((gs["result"] == "BỎ LỠ").sum())
            by_label[lab] = {"n": len(g), "scored": len(gs), "missed": missed,
                             "missed_rate": missed / len(gs) * 100 if len(gs) else None,
                             "mean_fwd": float(gs["fwd"].mean()) if len(gs) else None}
        else:
            by_label[lab] = {"total": len(g), **_acc_block(gs, base),
                             "mean_fwd": float(gs["fwd"].mean()) if len(gs) else None}
    confusion = {lab: {k: int(((scored["label"] == lab) & (scored["actual"] == k)).sum())
                       for k in ("TĂNG", "ĐI NGANG", "GIẢM")}
                 for lab in (*SCORED_LABELS, *WAIT_LABELS) if (scored["label"] == lab).any()}
    months = []
    for m, g in main.assign(month=main["date"].dt.to_period("M")).groupby("month"):
        blk = _acc_block(g, base)
        months.append({"month": f"{m.month:02d}/{m.year}", **blk})
    per_symbol = {}
    for s, g in cases.groupby("symbol", sort=False):
        last = g.sort_values("date").iloc[-1]
        today = last.get("label_flat") if isinstance(last.get("label_flat"), str) else last["label"]
        hist_same = main[(main["symbol"] == s) & (main["label"] == today)]
        wait_hist = scored[(scored["symbol"] == s) & (scored["label"] == today)] if today in WAIT_LABELS else None
        per_symbol[s] = {**_acc_block(main[main["symbol"] == s], base), "today": today,
                         "today_date": last["date"], "today_score": last["score"],
                         "today_hist": _acc_block(hist_same, base) if today in SCORED_LABELS else None,
                         "today_all": _acc_block(main[main["label"] == today], base) if today in SCORED_LABELS else None,
                         "today_missed": (int((wait_hist["result"] == "BỎ LỠ").sum()), len(wait_hist))
                         if wait_hist is not None else None}
    ok = main[main["result"] == "ĐÚNG"]
    bad = main[main["result"] == "SAI"]
    wait = scored[scored["result"] == "BỎ LỠ"]

    def pick(df, key):
        if not len(df):
            return None
        row = df.loc[df["fwd"].abs().idxmax()] if key == "abs" else df.loc[df["fwd"].idxmax()]
        return {k: row[k] for k in ("date", "symbol", "label", "fwd", "comment", "score")}
    unscored = cases[cases["actual"].isna()]
    return {
        "h": h, "overall": overall, "by_label": by_label, "confusion": confusion, "months": months,
        "per_symbol": per_symbol, "base": pooled,
        "n_cases": len(cases), "n_scored": len(scored), "n_unscored": len(unscored),
        "unscored_from": unscored["date"].min() if len(unscored) else None,
        "best_hit": pick(ok, "abs"), "worst_miss": pick(bad, "abs"), "biggest_missed": pick(wait, "max"),
        "trades": {"n": len(trades), "win": sum(t["net_pct"] > 0 for t in trades),
                   "avg": float(np.mean([t["net_pct"] for t in trades])) if trades else None},
    }


def horizon_table(sig: pd.DataFrame, band: float, missed_gain: float) -> list[dict]:
    """Độ chính xác chung ở cả 3 tầm nhìn 5/10/20 phiên (để thấy kết luận của bot đúng ở khung nào)."""
    out = []
    for h in HORIZONS:
        c = score_cases(sig, h, band, missed_gain)
        scored = c[c["actual"].notna()]
        base = _baseline_prob(scored) if len(scored) else {}
        out.append({"h": h, **_acc_block(scored[scored["label"].isin(SCORED_LABELS)], base)})
    return out


def _pct(x, digits=0) -> str:
    return "–" if x is None else f"{x:.{digits}f}%"


def commentary(acc: dict, symbols: list[str], start, end) -> list[str]:
    """Nhận xét bằng lời về quá khứ: đúng bao nhiêu case, loại kết luận nào đáng tin, case nổi bật."""
    h, o = acc["h"], acc["overall"]
    who = symbols[0] if len(symbols) == 1 else f"{len(symbols)} mã"
    lines = [f"Từ {start:%d/%m/%Y} đến {end:%d/%m/%Y}, bot ra {acc['n_cases']} kết luận cho {who}; "
             f"chấm được {acc['n_scored']} case bằng giá {h} phiên sau"
             + (f" ({acc['n_unscored']} phiên từ {acc['unscored_from']:%d/%m} chưa đủ tương lai)."
                if acc["n_unscored"] else ".")]
    if o["n"]:
        lines.append(f"Trên {o['n']} case có đúng/sai (BUY, SELL, NẮM GIỮ, TRÁNH MUA): đúng {o['correct']} "
                     f"= {o['acc']:.0f}%. Nếu đoán bừa theo tỷ lệ tăng/giảm tự nhiên của chính mã đó thì đúng "
                     f"khoảng {o['guess']:.0f}% → bot {'hơn' if o['diff'] >= 0 else 'kém'} {abs(o['diff']):.0f} "
                     f"điểm % → {_reliability_text(o)}.")
    else:
        lines.append("Không có case BUY/SELL/NẮM GIỮ/TRÁNH MUA nào đủ tương lai để chấm.")
    for lab in SCORED_LABELS:
        v = acc["by_label"].get(lab)
        if not v:
            continue
        if not v["n"]:
            lines.append(f"{lab}: {v['total']} lần, chưa lần nào đủ {h} phiên tương lai để chấm.")
            continue
        lines.append(f"{lab} ({RULES[lab]}): đúng {v['correct']}/{v['n']} = {v['acc']:.0f}% "
                     f"(đoán bừa {v['guess']:.0f}%, giá TB {v['mean_fwd']:+.1f}%) — {_reliability_text(v)}.")
    if "BUY" not in acc["by_label"]:
        lines.append("BUY: bot không phát BUY lần nào trong giai đoạn này nên CHƯA kiểm chứng được độ chính "
                     "xác của BUY — cần giai đoạn dài hơn hoặc nhiều mã hơn.")
    w = acc["by_label"].get("CHỜ")
    if w and w["scored"]:
        lines.append(f"CHỜ: {w['n']} lần; {w['missed']}/{w['scored']} lần ({w['missed_rate']:.0f}%) sau đó giá "
                     f"tăng > ngưỡng bỏ lỡ.")
    if acc["best_hit"]:
        b = acc["best_hit"]
        lines.append(f"Case đúng rõ nhất: {b['date']:%d/%m} {b['symbol']} {b['label']} — {b['comment']}")
    if acc["worst_miss"]:
        b = acc["worst_miss"]
        lines.append(f"Case sai nặng nhất: {b['date']:%d/%m} {b['symbol']} {b['label']} — {b['comment']}")
    if acc["biggest_missed"]:
        b = acc["biggest_missed"]
        lines.append(f"Bỏ lỡ lớn nhất: {b['date']:%d/%m} {b['symbol']} CHỜ (điểm {b['score']}) — {b['comment']}")
    good_months = [m for m in acc["months"] if m["n"] >= 15]
    if len(good_months) >= 2:
        hi = max(good_months, key=lambda m: m["acc"])
        lo = min(good_months, key=lambda m: m["acc"])
        lines.append(f"Theo tháng: tốt nhất {hi['month']} ({hi['acc']:.0f}% trên {hi['n']} case), "
                     f"kém nhất {lo['month']} ({lo['acc']:.0f}% trên {lo['n']} case).")
    lines.append(_trust_sentence(acc))
    return lines


def _reliability_text(block: dict) -> str:
    icon = block.get("icon")
    if not block.get("n"):
        return "chưa có case"
    if block["n"] < 10:
        return "quá ít case để tin"
    return {"✅": "tốt hơn đoán bừa một cách rõ ràng", "❌": "KÉM hơn đoán bừa một cách rõ ràng",
            "🟡": "có dấu hiệu " + ("tốt hơn" if (block.get("diff") or 0) >= 0 else "kém hơn")
                  + " đoán bừa nhưng chưa chắc", "⚪": "chưa khác đoán bừa, có thể chỉ do may rủi"}[icon]


def _trust_sentence(acc: dict) -> str:
    o = acc["overall"]
    if not o["n"] or o["n"] < 30:
        return ("KẾT LUẬN: chưa đủ case để đánh giá độ tin cậy — hãy chạy dài hơn (1–2 năm) hoặc nhiều mã hơn.")
    if o["icon"] == "✅":
        return (f"KẾT LUẬN: trong giai đoạn này, cách tính của bot ĐÁNG TIN hơn đoán bừa "
                f"({o['acc']:.0f}% so với {o['guess']:.0f}%). Vẫn nên kiểm tra thêm giai đoạn khác.")
    if o["icon"] == "❌":
        return (f"KẾT LUẬN: trong giai đoạn này, cách tính của bot KÉM hơn đoán bừa "
                f"({o['acc']:.0f}% so với {o['guess']:.0f}%) → cần xem lại quy tắc.")
    return (f"KẾT LUẬN: độ chính xác {o['acc']:.0f}% chưa khác biệt rõ so với đoán bừa {o['guess']:.0f}% → "
            "CHƯA đủ bằng chứng để tin cậy tín hiệu; cần thêm dữ liệu hoặc điều chỉnh chiến lược.")


def rescore(report: dict, h: int) -> dict:
    """Chấm lại cùng một lần replay ở tầm nhìn khác (không cần tải/dựng lại dữ liệu)."""
    cfg = report["validation_cfg"]
    cases = score_cases(report["signals"], h, cfg["flat_band"], cfg["missed_gain"])
    acc = accuracy_summary(cases, h, report["trades"])
    return {**report, "cases": cases, "accuracy": acc,
            "commentary": commentary(acc, report["symbols"], report["start"], report["end"])}


# =====================================================================
# 4. API chạy trọn + xuất báo cáo
# =====================================================================
def run(symbols=None, start=None, end=None, months: int | None = None, cfg: BacktestConfig = BACKTEST,
        progress=None, horizon: int | None = None) -> dict:
    """Load dữ liệu -> replay từng phiên -> chấm đúng/sai. Mặc định: 6 tháng trước tới hôm nay, 20 phiên."""
    symbols = [s.upper() for s in (symbols or cfg.symbols)]
    s, e = bt.resolve_period(start, end, months or VALIDATION.months, cfg)
    if not bt._run_lock.acquire(blocking=False):
        raise bt.BacktestBusy("Đang có một backtest/kiểm định khác chạy. Thử lại sau ít phút.")
    t0 = time.time()
    try:
        data = bt.load_data(symbols, s, e, cfg, progress=(lambda d, t, x: progress("data", d, t, x)) if progress else None)
        signals, trades, warn = replay_signals(
            data, s, e, cfg, progress=(lambda d, t, x: progress("sim", d, t, x)) if progress else None)
    finally:
        bt._run_lock.release()
    return build_report(data, signals, trades, s, e, data.warnings + warn, time.time() - t0, horizon=horizon)


def build_report(data, signals, trades, start, end, warnings, runtime, horizon: int | None = None,
                 vcfg=None) -> dict:
    vcfg = vcfg or VALIDATION
    h = horizon or vcfg.horizon
    sig = attach_forward_returns(signals, data)
    if sig.empty:
        raise ValueError("Không tạo được tín hiệu nào (thiếu dữ liệu giá?).")
    stats = evaluate_signals(sig, trades)
    last = sig[sig["date"] == sig["date"].max()]
    cases = score_cases(sig, h, vcfg.flat_band, vcfg.missed_gain)
    acc = accuracy_summary(cases, h, trades)
    first, final = sig["date"].min(), sig["date"].max()
    return {
        "cases": cases, "accuracy": acc,
        "commentary": commentary(acc, list(data.prices), first, final),
        "horizons": horizon_table(sig, vcfg.flat_band, vcfg.missed_gain),
        "validation_cfg": {"flat_band": vcfg.flat_band, "missed_gain": vcfg.missed_gain},
        "symbols": list(data.prices), "start": sig["date"].min().date(), "end": sig["date"].max().date(),
        "sessions": sig["date"].nunique(), "signals": sig, "trades": trades, "stats": stats,
        "verdicts": verdicts(stats), "warnings": warnings, "runtime_seconds": round(runtime, 1),
        "last": {r["symbol"]: {"label": r["label"], "score": r["score"], "market": r["market"]}
                 for r in last.to_dict("records")},
        "eval_until": sig.dropna(subset=[f"fwd{MAIN_H}"])["date"].max(),
    }


def crosscheck_live(report: dict, analyze, max_symbols: int = 3) -> list[dict]:
    """So phiên cuối của replay với engine live (stock_service.analyze_symbol) — kiểm tra pipeline.

    Điểm không phụ thuộc vị thế nên phải trùng tuyệt đối nếu cùng dữ liệu.
    """
    out = []
    for s in report["symbols"][:max_symbols]:
        mine = report["last"].get(s)
        if not mine:
            continue
        try:
            live = analyze(s)
        except Exception as exc:
            out.append({"symbol": s, "ok": None, "detail": f"không gọi được engine live: {type(exc).__name__}"})
            continue
        same = (live.get("score_min") == mine["score"]
                and (live.get("market") or {}).get("state") == mine["market"])
        out.append({"symbol": s, "ok": same, "replay": mine["score"], "live": live.get("score_min"),
                    "detail": f"replay {mine['score']}/100 {mine['market']} · live {live.get('score_min')}/100 "
                              f"{(live.get('market') or {}).get('state')}"})
    return out


SIGNAL_COLUMNS = [("date", "ngay"), ("symbol", "ma"), ("market", "thi_truong"), ("label", "ket_luan"),
                  ("score", "diem"), ("threshold", "nguong"), ("t1", "T1"), ("t2", "T2"), ("t3", "T3"),
                  ("in_position", "dang_giu_ao"), ("close", "gia_dong_cua"), ("fwd5", "loi_nhuan_5p_pct"),
                  ("fwd10", "loi_nhuan_10p_pct"), ("fwd20", "loi_nhuan_20p_pct"), ("vni20", "vnindex_20p_pct"),
                  ("ex20", "vuot_vnindex_20p"), ("correct", "dung_sai_20p"), ("reason", "ly_do")]


def signals_csv(report: dict) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([n for _, n in SIGNAL_COLUMNS])
    for r in report["signals"].to_dict("records"):
        row = []
        for k, _ in SIGNAL_COLUMNS:
            v = r.get(k)
            if k == "date":
                v = pd.Timestamp(v).strftime("%Y-%m-%d")
            elif k == "correct":
                v = "" if v is None or (isinstance(v, float) and math.isnan(v)) else ("ĐÚNG" if v else "SAI")
            elif isinstance(v, (bool, np.bool_)):
                v = "x" if v else ""
            elif isinstance(v, float):
                v = "" if math.isnan(v) else round(v, 2)
            row.append("" if v is None else v)
        w.writerow(row)
    return buf.getvalue().encode("utf-8-sig")


def trades_csv(report: dict) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    cols = ["symbol", "buy_signal", "entry_date", "entry_price", "sell_signal", "exit_date", "exit_price",
            "return_pct", "net_pct", "sessions", "reason"]
    w.writerow(cols)
    for t in report["trades"]:
        w.writerow([pd.Timestamp(t[c]).strftime("%Y-%m-%d") if c.endswith(("_date", "_signal")) and t[c] is not None
                    else (round(t[c], 2) if isinstance(t[c], float) else ("" if t[c] is None else t[c]))
                    for c in cols])
    return buf.getvalue().encode("utf-8-sig")


# =====================================================================
# 5. Biểu đồ
# =====================================================================
def render_summary_png(report: dict) -> bytes:
    """3 biểu đồ cột: lợi nhuận 20 phiên sau theo loại tín hiệu | theo nhóm điểm | VN-Index theo cổng thị trường."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    from chart_service import COLORS, _render_lock, _style_axis

    st = report["stats"]
    base = st["base"].get(f"mean{MAIN_H}") or 0.0
    lab_colors = {"BUY": COLORS["up"], "SELL": COLORS["down"], "NẮM GIỮ": COLORS["ma50"], "CHỜ": COLORS["muted"],
                  "CHỜ DỮ LIỆU": COLORS["muted"], "TRÁNH MUA": COLORS["ma150"]}
    with _render_lock:
        fig = Figure(figsize=(13, 5.6), dpi=130, facecolor=COLORS["bg"])
        FigureCanvasAgg(fig)
        axes = fig.subplots(1, 3, gridspec_kw={"wspace": 0.28, "left": 0.05, "right": 0.97,
                                               "top": 0.8, "bottom": 0.16})
        panels = [
            ("Theo loại tín hiệu", [(k, v) for k, v in st["by_label"].items()], lab_colors, True),
            ("Theo nhóm điểm (khi chưa giữ)", [(b["range"], b) for b in st["buckets"]], None, True),
            ("VN-Index theo cổng thị trường",
             [(k, {f"mean{MAIN_H}": v["mean"], "n_eval": v["n_eval"]}) for k, v in st["gates"].items()], None, False),
        ]
        for ax, (title, items, colors, show_base) in zip(axes, panels):
            _style_axis(ax)
            ax.yaxis.tick_left()
            names = [k for k, _ in items]
            vals = [to_float(v.get(f"mean{MAIN_H}"), 0.0) for _, v in items]
            ns = [v.get("n_eval", 0) for _, v in items]
            cols = [colors.get(k, COLORS["ma20"]) if colors else (COLORS["up"] if x >= 0 else COLORS["down"])
                    for k, x in zip(names, vals)]
            x = np.arange(len(names))
            ax.bar(x, vals, color=cols, alpha=0.85, width=0.62, zorder=3)
            ax.axhline(0, color=COLORS["muted"], linewidth=0.8)
            if show_base:
                ax.axhline(base, color=COLORS["ma20"], linewidth=1.0, linestyle=(0, (4, 3)))
                ax.text(len(names) - 0.5, base, f" mốc {base:+.1f}%", color=COLORS["ma20"], fontsize=7.5,
                        va="bottom", ha="right")
            for xi, v, n in zip(x, vals, ns):
                ax.text(xi, v, f"{v:+.1f}%\nn={n}", ha="center", va="bottom" if v >= 0 else "top",
                        fontsize=7.5, color=COLORS["text"])
            ax.set_xticks(x)
            ax.set_xticklabels(names, fontsize=8, color=COLORS["text"])
            ax.set_title(title, color=COLORS["text"], fontsize=10)
            lo, hi = min(vals + [0, base]), max(vals + [0, base])
            pad = (hi - lo) * 0.25 or 1
            ax.set_ylim(lo - pad, hi + pad)
        fig.text(0.05, 0.93, f"KIỂM ĐỊNH TÍN HIỆU QUÁ KHỨ · {len(report['symbols'])} mã · "
                             f"{report['start']:%d/%m/%Y}→{report['end']:%d/%m/%Y}",
                 fontsize=14, fontweight="bold", color=COLORS["text"])
        fig.text(0.05, 0.875, f"Lợi nhuận TB {MAIN_H} phiên sau tín hiệu (close t → close t+{MAIN_H}) · "
                              f"đường vàng = mốc trung bình mọi (mã, phiên)",
                 fontsize=9, color=COLORS["muted"])
        fig.text(0.97, 0.03, "In-sample · kết quả quá khứ không đảm bảo tương lai", fontsize=7,
                 color=COLORS["muted"], ha="right")
        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=COLORS["bg"])
    return buf.getvalue()


def render_timeline_png(rows: pd.DataFrame, symbol: str, trades: list[dict] | None = None,
                        title: str | None = None) -> bytes:
    """Dòng thời gian một mã: giá + điểm BUY/SELL, điểm số vs ngưỡng, trạng thái thị trường.

    `rows` cần các cột date, close, label, score, threshold, market (từ kiểm định hoặc nhật ký backtest).
    """
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib.ticker import FuncFormatter, MaxNLocator

    from chart_service import COLORS, _render_lock, _style_axis

    r = rows[rows["symbol"] == symbol].sort_values("date").reset_index(drop=True)
    if r.empty:
        raise ValueError(f"Không có tín hiệu của {symbol}")
    n = len(r)
    x = np.arange(n)
    close = pd.to_numeric(r["close"], errors="coerce").to_numpy(float)
    score = pd.to_numeric(r["score"], errors="coerce").to_numpy(float)
    thr = pd.to_numeric(r["threshold"], errors="coerce").to_numpy(float)
    gate_color = {"UPTREND": COLORS["up"], "PRESSURE": "#ff9800", "WEAK": COLORS["ma20"], "DOWNTREND": COLORS["down"]}
    with _render_lock:
        fig = Figure(figsize=(12, 7.2), dpi=140, facecolor=COLORS["bg"])
        FigureCanvasAgg(fig)
        grid = fig.add_gridspec(3, 1, height_ratios=[4, 2.2, 0.35], hspace=0.08,
                                left=0.05, right=0.93, top=0.86, bottom=0.08)
        ax1, ax2, ax3 = (fig.add_subplot(grid[0]), fig.add_subplot(grid[1]), fig.add_subplot(grid[2]))
        for ax in (ax1, ax2, ax3):
            _style_axis(ax)
        ax2.sharex(ax1)
        ax3.sharex(ax1)
        ax1.plot(x, close, color=COLORS["text"], linewidth=1.3, label="Giá đóng cửa")
        for lab, marker, color, name in (("BUY", "^", COLORS["up"], "BUY"), ("SELL", "v", COLORS["down"], "SELL")):
            m = (r["label"] == lab).to_numpy()
            if m.any():
                ax1.scatter(x[m], close[m], marker=marker, s=60, color=color, zorder=5, label=name)
        avoid = (r["label"] == "TRÁNH MUA").to_numpy()
        hold = (r["label"] == "NẮM GIỮ").to_numpy()
        lo, hi = np.nanmin(close), np.nanmax(close)
        if avoid.any():
            ax1.fill_between(x, lo, hi, where=avoid, color=COLORS["ma150"], alpha=0.10, step="mid", label="Tránh mua")
        if hold.any():
            ax1.fill_between(x, lo, hi, where=hold, color=COLORS["up"], alpha=0.12, step="mid", label="Đang giữ")
        ax1.legend(loc="upper left", frameon=False, fontsize=8, labelcolor=COLORS["text"], ncol=5)
        ax1.yaxis.set_major_formatter(FuncFormatter(lambda v, _p: f"{v:,.0f}"))

        ax2.plot(x, score, color=COLORS["ma20"], linewidth=1.4, label="Điểm (0–100)")
        ax2.step(x, thr, where="mid", color=COLORS["down"], linewidth=1.1, linestyle=(0, (4, 2)),
                 label="Ngưỡng BUY (60 UPTREND / 75 PRESSURE)")
        best = int(np.nanargmax(score)) if np.isfinite(score).any() else None
        if best is not None:
            ax2.annotate(f"cao nhất {score[best]:.0f}\n{r['date'].iloc[best]:%d/%m}", xy=(best, score[best]),
                         xytext=(0, 8), textcoords="offset points", ha="center", fontsize=7.5, color=COLORS["text"])
        ax2.set_ylim(0, 105)
        ax2.legend(loc="upper left", frameon=False, fontsize=8, labelcolor=COLORS["text"], ncol=2)

        for i, g in enumerate(r["market"]):
            ax3.axvspan(i - 0.5, i + 0.5, color=gate_color.get(g, COLORS["muted"]), alpha=0.8, linewidth=0)
        ax3.set_yticks([])
        ax3.text(1.005, 0.5, "Thị trường", transform=ax3.transAxes, fontsize=7.5, color=COLORS["muted"], va="center")
        dates = pd.to_datetime(r["date"])
        ax3.xaxis.set_major_locator(MaxNLocator(8, integer=True))
        ax3.xaxis.set_major_formatter(FuncFormatter(
            lambda v, _p: dates.iloc[int(round(v))].strftime("%d/%m/%y") if 0 <= round(v) < n else ""))
        ax1.tick_params(labelbottom=False)
        ax2.tick_params(labelbottom=False)
        ax1.set_xlim(-1, n)
        counts = Counter(r["label"])
        fig.text(0.05, 0.945, title or f"KIỂM ĐỊNH TÍN HIỆU · {symbol}", fontsize=15, fontweight="bold",
                 color=COLORS["text"])
        fig.text(0.05, 0.9, f"{dates.iloc[0]:%d/%m/%Y} → {dates.iloc[-1]:%d/%m/%Y} · BUY {counts.get('BUY', 0)} · "
                            f"SELL {counts.get('SELL', 0)} · Tránh mua {counts.get('TRÁNH MUA', 0)} · "
                            f"Chờ {counts.get('CHỜ', 0) + counts.get('CHỜ DỮ LIỆU', 0)} phiên · "
                            "dải màu dưới cùng: xanh UPTREND, cam PRESSURE, vàng WEAK, đỏ DOWNTREND",
                 fontsize=9, color=COLORS["muted"])
        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=COLORS["bg"])
    return buf.getvalue()


RESULT_STYLE = {"ĐÚNG": ("#26a69a", "Đúng"), "SAI": ("#ef5350", "Sai"), "BỎ LỠ": ("#ff9800", "Chờ – bỏ lỡ"),
                "KHÔNG BỎ LỠ": ("#787b86", "Chờ – không bỏ lỡ"), "CHƯA CHẤM": ("#d1d4dc", "Chưa chấm")}
LABEL_MARKER = {"BUY": "^", "SELL": "v", "NẮM GIỮ": "s", "TRÁNH MUA": "X", "CHỜ": "o", "CHỜ DỮ LIỆU": "o"}


def render_accuracy_png(report: dict, symbol: str) -> bytes:
    """Mỗi phiên là một chấm trên đường giá: màu = đúng/sai theo giá h phiên sau, hình = kết luận của bot.
    Tầng dưới: tỷ lệ đúng trượt 20 case gần nhất so với mức đoán bừa."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib.lines import Line2D
    from matplotlib.ticker import FuncFormatter, MaxNLocator

    from chart_service import COLORS, _render_lock, _style_axis

    acc = report["accuracy"]
    h = acc["h"]
    c = report["cases"]
    c = c[c["symbol"] == symbol].sort_values("date").reset_index(drop=True)
    if c.empty:
        raise ValueError(f"Không có case của {symbol}")
    n = len(c)
    x = np.arange(n)
    close = pd.to_numeric(c["close"], errors="coerce").to_numpy(float)
    judged = c["result"].isin(["ĐÚNG", "SAI"]).to_numpy()
    # Tỷ lệ đúng trượt trên 20 CASE ĐƯỢC CHẤM gần nhất (bỏ qua các phiên CHỜ / chưa chấm).
    jx = x[judged]
    roll = (pd.Series((c.loc[judged, "result"] == "ĐÚNG").astype(float).to_numpy())
            .rolling(20, min_periods=8).mean().to_numpy() * 100)
    ps = acc["per_symbol"].get(symbol) or {}
    with _render_lock:
        fig = Figure(figsize=(12, 7.2), dpi=140, facecolor=COLORS["bg"])
        FigureCanvasAgg(fig)
        grid = fig.add_gridspec(2, 1, height_ratios=[4, 1.8], hspace=0.08, left=0.05, right=0.93, top=0.85, bottom=0.08)
        ax1, ax2 = fig.add_subplot(grid[0]), fig.add_subplot(grid[1])
        for ax in (ax1, ax2):
            _style_axis(ax)
        ax2.sharex(ax1)
        ax1.plot(x, close, color=COLORS["muted"], linewidth=1.0, zorder=1)
        for lab, marker in LABEL_MARKER.items():
            for res, (color, _) in RESULT_STYLE.items():
                m = ((c["label"] == lab) & (c["result"] == res)).to_numpy()
                if m.any():
                    size = 34 if res in ("ĐÚNG", "SAI") else 16
                    face = "none" if res == "CHƯA CHẤM" else color
                    ax1.scatter(x[m], close[m], marker=marker, s=size, facecolors=face, edgecolors=color,
                                linewidths=0.8, zorder=3)
        handles = [Line2D([], [], linestyle="", marker="o", color=col, label=name)
                   for res, (col, name) in RESULT_STYLE.items() if (c["result"] == res).any()]
        handles += [Line2D([], [], linestyle="", marker=LABEL_MARKER[lab], color=COLORS["text"], label=lab)
                    for lab in ("BUY", "SELL", "NẮM GIỮ", "TRÁNH MUA", "CHỜ") if (c["label"] == lab).any()]
        ax1.legend(handles=handles, loc="upper left", frameon=False, fontsize=7.8, labelcolor=COLORS["text"], ncol=5)
        ax1.yaxis.set_major_formatter(FuncFormatter(lambda v, _p: f"{v:,.0f}"))

        ax2.plot(jx, roll, color=COLORS["ma20"], linewidth=1.5, marker=".", markersize=3,
                 label="Tỷ lệ đúng (20 case chấm gần nhất)")
        if ps.get("guess") is not None:
            ax2.axhline(ps["guess"], color=COLORS["ma50"], linewidth=1.0, linestyle=(0, (4, 3)),
                        label=f"Đoán bừa ≈ {ps['guess']:.0f}%")
        ax2.axhline(50, color=COLORS["grid"], linewidth=0.8)
        ax2.set_ylim(0, 105)
        ax2.yaxis.set_major_formatter(FuncFormatter(lambda v, _p: f"{v:.0f}%"))
        ax2.legend(loc="lower left", frameon=False, fontsize=8, labelcolor=COLORS["text"], ncol=2)
        dates = pd.to_datetime(c["date"])
        ax2.xaxis.set_major_locator(MaxNLocator(8, integer=True))
        ax2.xaxis.set_major_formatter(FuncFormatter(
            lambda v, _p: dates.iloc[int(round(v))].strftime("%d/%m/%y") if 0 <= round(v) < n else ""))
        ax1.tick_params(labelbottom=False)
        ax1.set_xlim(-1, n)
        head = (f"{symbol} · bot đúng {ps.get('correct', 0)}/{ps.get('n', 0)} case"
                + (f" = {ps['acc']:.0f}%" if ps.get("acc") is not None else ""))
        fig.text(0.05, 0.94, head, fontsize=15, fontweight="bold", color=COLORS["text"])
        fig.text(0.05, 0.895, f"{dates.iloc[0]:%d/%m/%Y} → {dates.iloc[-1]:%d/%m/%Y} · chấm bằng giá {h} phiên sau · "
                              "BUY đúng khi tăng, SELL khi giảm tiếp, NẮM GIỮ khi không giảm, TRÁNH MUA khi không tăng",
                 fontsize=8.5, color=COLORS["muted"])
        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=COLORS["bg"])
    return buf.getvalue()


def render_accuracy_summary_png(report: dict) -> bytes:
    """3 biểu đồ: tỷ lệ đúng theo kết luận vs đoán bừa | theo tháng | theo tầm nhìn 5/10/20 phiên."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    from chart_service import COLORS, _render_lock, _style_axis

    acc = report["accuracy"]
    with _render_lock:
        fig = Figure(figsize=(13, 5.4), dpi=130, facecolor=COLORS["bg"])
        FigureCanvasAgg(fig)
        axes = fig.subplots(1, 3, gridspec_kw={"wspace": 0.25, "left": 0.05, "right": 0.98, "top": 0.8, "bottom": 0.15,
                                               "width_ratios": [1.1, 1.4, 0.8]})
        labs = [(k, v) for k, v in acc["by_label"].items() if k in SCORED_LABELS and v["n"]]
        panels = [
            ("Tỷ lệ đúng theo kết luận", [(k, v) for k, v in labs]),
            ("Tỷ lệ đúng theo tháng", [(m["month"], m) for m in acc["months"] if m["n"]]),
            ("Theo tầm nhìn chấm", [(f"{r['h']} phiên", r) for r in report.get("horizons") or [] if r["n"]]),
        ]
        for ax, (title, items) in zip(axes, panels):
            _style_axis(ax)
            ax.yaxis.tick_left()
            ax.set_title(title, color=COLORS["text"], fontsize=10)
            ax.set_ylim(0, 110)
            if not items:
                ax.text(0.5, 0.5, "Chưa có case chấm được", transform=ax.transAxes, ha="center",
                        color=COLORS["muted"])
                continue
            xs = np.arange(len(items))
            a = [v["acc"] for _, v in items]
            g = [v["guess"] for _, v in items]
            ax.bar(xs - 0.2, a, width=0.4, color=COLORS["up"], label="Bot đúng", zorder=3)
            ax.bar(xs + 0.2, g, width=0.4, color=COLORS["muted"], alpha=0.7, label="Đoán bừa", zorder=3)
            many = len(items) > 7
            for xi, v, (_, blk) in zip(xs, a, items):
                ax.text(xi - 0.2, v + 1, f"{v:.0f}%" if many else f"{v:.0f}%\n{blk['correct']}/{blk['n']}",
                        ha="center", va="bottom", fontsize=6.5 if many else 7, color=COLORS["text"])
            ax.set_xticks(xs)
            ax.set_xticklabels([k for k, _ in items], fontsize=7.5, color=COLORS["text"],
                               rotation=30 if len(items) > 5 else 0)
            ax.axhline(50, color=COLORS["grid"], linewidth=0.8)
            ax.legend(loc="upper right", frameon=False, fontsize=7.5, labelcolor=COLORS["text"])
        o = acc["overall"]
        scope = report["symbols"][0] if len(report["symbols"]) == 1 else f"{len(report['symbols'])} mã"
        head = (f"KIỂM ĐỊNH ĐỘ CHÍNH XÁC · {scope} · bot đúng {o['correct']}/{o['n']} case"
                + (f" = {o['acc']:.0f}% (đoán bừa {o['guess']:.0f}%)" if o["n"] else ""))
        fig.text(0.05, 0.93, head, fontsize=13.5, fontweight="bold", color=COLORS["text"])
        fig.text(0.05, 0.875, f"Tín hiệu {report['start']:%d/%m/%Y} → {report['end']:%d/%m/%Y} · chấm bằng giá "
                              f"{acc['h']} phiên sau · đoán bừa = tỷ lệ tăng/giảm tự nhiên của chính mã đó",
                 fontsize=9, color=COLORS["muted"])
        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=COLORS["bg"])
    return buf.getvalue()


CASE_COLUMNS = [("date", "ngay"), ("symbol", "ma"), ("close", "gia_dong_cua"), ("market", "thi_truong"),
                ("label", "ket_luan_cua_bot"), ("score", "diem"), ("threshold", "nguong_buy"),
                ("fwd", "thay_doi_gia_sau_H_phien_pct"), ("actual", "thuc_te"), ("result", "danh_gia"),
                ("comment", "nhan_xet"), ("fwd5", "sau_5p_pct"), ("fwd10", "sau_10p_pct"), ("fwd20", "sau_20p_pct"),
                ("vni20", "vnindex_20p_pct"), ("reason", "ly_do_cua_bot")]


def cases_csv(report: dict) -> bytes:
    """Mỗi dòng một case: kết luận của bot, giá tương lai, nhãn thật, ĐÚNG/SAI và nhận xét."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([n for _, n in CASE_COLUMNS])
    for r in report["cases"].to_dict("records"):
        row = []
        for k, _ in CASE_COLUMNS:
            v = r.get(k)
            if k == "date":
                v = pd.Timestamp(v).strftime("%Y-%m-%d")
            elif isinstance(v, float):
                v = "" if math.isnan(v) else round(v, 2)
            row.append("" if v is None else v)
        w.writerow(row)
    return buf.getvalue().encode("utf-8-sig")


# =====================================================================
# 6. CLI
# =====================================================================
def summary_text(report: dict) -> str:
    acc = report["accuracy"]
    lines = [f"KIỂM ĐỊNH ĐỘ CHÍNH XÁC · {', '.join(report['symbols'])}",
             f"Tín hiệu {report['start']:%d/%m/%Y} -> {report['end']:%d/%m/%Y}, chấm bằng giá {acc['h']} phiên sau", "",
             f"{'Kết luận':<11}{'Quy tắc đúng':<16}{'Đúng/Chấm':>11}{'Đúng%':>7}{'Đoán bừa':>10}  Tin cậy"]
    for lab, v in acc["by_label"].items():
        if lab in SCORED_LABELS:
            lines.append(f"{lab:<11}{RULES[lab]:<16}{str(v['correct']) + '/' + str(v['n']):>11}"
                         f"{_p(v['acc']):>7}{_p(v['guess']):>10}  {_reliability_text(v)}")
        else:
            lines.append(f"{lab:<11}{'bỏ lỡ (>' + format(MISSED_GAIN, 'g') + '%)':<16}"
                         f"{str(v['missed']) + '/' + str(v['scored']):>11}{_p(v['missed_rate']):>7}")
    o = acc["overall"]
    lines += [f"{'TỔNG':<11}{'':<16}{str(o['correct']) + '/' + str(o['n']):>11}{_p(o['acc']):>7}{_p(o['guess']):>10}",
              "", "Theo tầm nhìn: " + " · ".join(f"{r['h']} phiên {_p(r['acc'])} (bừa {_p(r['guess'])})"
                                               for r in report["horizons"]),
              "", "Theo tháng:"] + [f"  {m['month']}: {m['correct']}/{m['n']} = {_p(m['acc'])}" for m in acc["months"]]
    lines += ["", "Hôm nay:"] + [
        f"  {s}: {v['today']} ({v['today_score']}/100)"
        + (f" — quá khứ khi nói {v['today']}: đúng {v['today_hist']['correct']}/{v['today_hist']['n']}"
           if v.get("today_hist") and v["today_hist"]["n"] else "")
        for s, v in acc["per_symbol"].items()]
    lines += ["", "Nhận xét:"] + [f"  - {x}" for x in report["commentary"]]
    lines += ["", "Phân tích nâng cao (IC, cổng thị trường):"] + [f"  {a} {b}" for a, b in report["verdicts"]]
    if report["warnings"]:
        lines += ["", "Cảnh báo:"] + [f"  - {w}" for w in report["warnings"]]
    return "\n".join(lines)


def _f(x) -> str:
    return "–" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:+.2f}%"


def _p(x) -> str:
    return "–" if x is None else f"{x:.0f}%"


def _ic(x) -> str:
    return "N/A" if x is None else f"{x:+.3f}"


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Kiểm định tín hiệu BUY/HOLD/SELL trên dữ liệu quá khứ")
    parser.add_argument("--symbols")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--period", choices=list(PERIOD_MONTHS), default="6M")
    parser.add_argument("--horizon", type=int, choices=list(HORIZONS), default=VALIDATION.horizon,
                        help="Chấm đúng/sai bằng giá sau N phiên")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

    def show(stage, done, total, text):
        if stage == "data" or done == total or done % 500 == 0:
            print(f"[{stage} {done}/{total}] {text}", flush=True)

    symbols = [x.strip().upper() for x in args.symbols.split(",")] if args.symbols else None
    rep = run(symbols, args.start, args.end, None if args.start else PERIOD_MONTHS[args.period], progress=show,
              horizon=args.horizon)
    folder = bt.REPORT_DIR / f"validation_{datetime.now():%Y%m%d_%H%M%S}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "summary.txt").write_text(summary_text(rep), encoding="utf-8")
    (folder / "cases.csv").write_bytes(cases_csv(rep))
    (folder / "virtual_trades.csv").write_bytes(trades_csv(rep))
    (folder / "accuracy.png").write_bytes(render_accuracy_summary_png(rep))
    (folder / "advanced.png").write_bytes(render_summary_png(rep))
    for s in rep["symbols"]:
        (folder / f"accuracy_{s}.png").write_bytes(render_accuracy_png(rep, s))
    print(summary_text(rep))
    print(f"\nĐã lưu tại {folder}")


if __name__ == "__main__":
    main()
