# -*- coding: utf-8 -*-
"""PDF equity-analysis report (bố cục kiểu báo cáo công ty chứng khoán).

Dùng cùng đối tượng `result` với web. Không tính lại điểm hay tín hiệu trong PDF.
Giữ nguyên chữ ký hàm: build_report_pdf(result, *, include_chart=True) -> bytes

Trang 1: Tóm tắt | Trang 2: Phân tích cơ bản | Trang 3: Kỹ thuật + tin tức |
Trang 4: Chấm điểm + kết luận (+ bảng nguồn nếu đủ chỗ; nếu không sẽ sang trang 5).
"""
from __future__ import annotations

import io
import math
import textwrap
import re
import threading
import time
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.image as mpimg
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from matplotlib import rcParams
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.patches import FancyBboxPatch, Wedge, Rectangle, Circle

import chart_service as charts
from utils import to_float

# Times New Roman trên Windows; máy thiếu font sẽ dùng Liberation Serif tương đương.
from matplotlib import font_manager
_FONT_FILES = [
    Path('C:/Windows/Fonts/times.ttf'),
    Path('C:/Windows/Fonts/timesbd.ttf'),
    Path('C:/Windows/Fonts/timesi.ttf'),
    Path('C:/Windows/Fonts/timesbi.ttf'),
]
for _font in _FONT_FILES:
    if _font.is_file():
        font_manager.fontManager.addfont(str(_font))
_FONT_FAMILY = 'Times New Roman' if _FONT_FILES[0].is_file() else 'Liberation Serif'
rcParams['font.family'] = _FONT_FAMILY
rcParams['font.serif'] = [_FONT_FAMILY, 'DejaVu Serif']
rcParams["pdf.fonttype"] = 42

# ---------------- Bảng màu ----------------
NAVY = "#082B60"
NAVY_DEFAULT = NAVY
BLUE = "#1679CC"
LIGHT = "#EEF3FB"
GRID = "#D5DDEA"
INK = "#1B2433"
MUTED = "#6B7689"
GREEN, GREEN_BG = "#1E9E63", "#E8F6EF"
AMBER, AMBER_BG = "#D98A0B", "#FFF4DD"
RED, RED_BG = "#D64545", "#FDECEC"
PURPLE = "#7A4FD0"

W, H = 210.0, 297.0  # A4 (mm)

SIGNAL_COLORS = {"BUY": (GREEN, GREEN_BG), "HOLD": (AMBER, AMBER_BG), "SELL": (RED, RED_BG)}
RISK_COLORS = {"Thấp": GREEN, "Trung bình": AMBER, "Cao": RED}
FUND_KEYS = ("revenue_growth", "profit_growth", "roe", "roa", "de", "pe", "pb")
GROUP_OF = {"revenue_growth": "Tăng trưởng", "profit_growth": "Tăng trưởng", "roe": "Sinh lời",
            "roa": "Sinh lời", "pe": "Định giá", "pb": "Định giá", "de": "Đòn bẩy"}


# ---------------- Định dạng ----------------
def _fmt(value, unit=""):
    v = to_float(value)
    if v is None:
        return "N/A"
    if unit == "%":
        return f"{v:+.1f}%"
    if unit == "x":
        return f"{v:.2f}x"
    return f"{v:,.0f}" if abs(v) >= 1000 else f"{v:.2f}"


def _ty(value):
    """VND -> tỷ đồng (chuỗi). Giá trị nhỏ hơn 1e7 coi như đã ở đơn vị tỷ."""
    v = to_float(value)
    if v is None:
        return "N/A"
    if abs(v) >= 1e7:
        v = v / 1e9
    return f"{v:,.0f}"


def _ty_num(value):
    v = to_float(value)
    if v is None:
        return 0.0
    return v / 1e9 if abs(v) >= 1e7 else v


# ---------------- Hàm vẽ cơ bản ----------------
def new_page():
    fig = plt.figure(figsize=(W / 25.4, H / 25.4), dpi=130)
    fig.patch.set_facecolor("white")
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, W)
    ax.set_ylim(H, 0)
    ax.axis("off")
    return fig, ax


def sub_axes(fig, x, y, w, h):
    return fig.add_axes([x / W, 1 - (y + h) / H, w / W, h / H])


def box(ax, x, y, w, h, fc="white", ec=GRID, r=2.0, lw=0.8, z=1):
    ax.add_patch(FancyBboxPatch((x + r, y + r), w - 2 * r, h - 2 * r,
                                boxstyle=f"round,pad={r},rounding_size={r}",
                                fc=fc, ec=ec, lw=lw, zorder=z))


def txt(ax, x, y, s, size=8, color=INK, weight="normal", ha="left", va="top",
        style="normal", z=5):
    return ax.text(x, y, s, fontsize=size, color=color, fontweight=weight,
                   ha=ha, va=va, fontstyle=style, zorder=z)


def wrap(s, w_mm, size):
    chars = max(8, int(w_mm / (size * 0.3528 * 0.56)))
    return textwrap.wrap(str(s or ""), chars) or [""]


def para(ax, x, y, s, w, size=7.5, color=INK, lead=1.55, weight="normal", max_lines=None):
    lh = size * 0.3528 * lead
    lines = wrap(s, w, size)
    if max_lines and len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(" .,;") + "…"
    for line in lines:
        txt(ax, x, y, line, size, color, weight)
        y += lh
    return y


def bullets(ax, x, y, items, w, size=7.3, dot=INK, color=INK, gap=1.2, max_y=None):
    lh = size * 0.3528 * 1.5
    if not items:
        items = ["Chưa có mục nổi bật từ dữ liệu hiện có."]
    for it in items:
        lines = wrap(it, w - 4, size)
        if max_y is not None and y + len(lines) * lh > max_y:
            break
        ax.add_patch(Circle((x + 1.0, y + lh * 0.48), 0.65, fc=dot, ec="none", zorder=5))
        for line in lines:
            txt(ax, x + 3.2, y, line, size, color)
            y += lh
        y += gap
    return y


def card_title(ax, x, y, s, color=NAVY, size=11):
    txt(ax, x, y, s, size, color, "bold")


def header(ax, num, title, ticker, date_s, brand=None):
    NAVY = (brand or {}).get("primary", NAVY_DEFAULT)
    ax.add_patch(Rectangle((0, 0), W, 14, fc=NAVY, ec="none", zorder=1))
    if num is None:
        txt(ax, 10, 7, "Stock Opportunity Analyzer", 12, "white", "bold", va="center")
        txt(ax, 10, 11.2, "Phân tích thông minh · Kiến tạo tương lai", 5.5, "#AFC3E6", va="center")
        txt(ax, W - 10, 5.6, "BÁO CÁO PHÂN TÍCH CƠ HỘI ĐẦU TƯ", 8.5, "white", "bold", ha="right", va="center")
        txt(ax, W - 10, 10.6, f"Ngày lập báo cáo: {date_s}", 6, "#AFC3E6", ha="right", va="center")
    else:
        box(ax, 10, 3, 8, 8, fc=BLUE, ec=BLUE, r=1.5, z=2)
        txt(ax, 14, 7.1, str(num), 10, "white", "bold", ha="center", va="center")
        txt(ax, 21, 7.1, title, 11, "white", "bold", va="center")
        txt(ax, W - 10, 7.1, f"{ticker}  |  {date_s}", 7, "#AFC3E6", ha="right", va="center")


def footer(ax, d, page_no):
    ax.plot([10, W - 10], [286, 286], color=GRID, lw=0.6)
    txt(ax, 10, 289.5, f"{d['ticker']} · Báo cáo phân tích cơ hội đầu tư · {d['date']}", 6, MUTED)
    txt(ax, W / 2, 289.5, "NHÓM 1", 8, NAVY, "bold", ha="center")
    txt(ax, W - 10, 289.5, f"Trang {page_no} / {d['_total']}", 6, MUTED, ha="right")


def table(ax, x, y, widths, head, rows, size=7, row_h=6.2, aligns=None,
          head_fc=NAVY, zebra=True, bold_last=False, wrap_cols=(), colors=None):
    aligns = aligns or ["left"] * len(widths)
    tw = sum(widths)
    ax.add_patch(Rectangle((x, y), tw, row_h, fc=head_fc, ec="none", zorder=2))
    cx = x
    for i, h in enumerate(head):
        tx = cx + 2 if aligns[i] == "left" else (cx + widths[i] / 2 if aligns[i] == "center" else cx + widths[i] - 2)
        txt(ax, tx, y + row_h / 2, h, size, "white", "bold", ha=aligns[i], va="center")
        cx += widths[i]
    y += row_h
    lh = size * 0.3528 * 1.45
    for ri, row in enumerate(rows):
        lines_per = [wrap(str(c), widths[ci] - 4, size) if ci in wrap_cols else [str(c)]
                     for ci, c in enumerate(row)]
        n = max(len(l) for l in lines_per)
        rh = max(row_h, n * lh + 2.4)
        if zebra and ri % 2 == 1:
            ax.add_patch(Rectangle((x, y), tw, rh, fc="#F6F8FC", ec="none", zorder=1))
        last = bold_last and ri == len(rows) - 1
        if last:
            ax.add_patch(Rectangle((x, y), tw, rh, fc=LIGHT, ec="none", zorder=1))
        cx = x
        for ci, lines in enumerate(lines_per):
            col = (colors or {}).get((ri, ci), INK)
            for li, line in enumerate(lines):
                ty = y + (rh - len(lines) * lh) / 2 + li * lh + 0.2
                tx = cx + 2 if aligns[ci] == "left" else (cx + widths[ci] / 2 if aligns[ci] == "center" else cx + widths[ci] - 2)
                txt(ax, tx, ty, line, size, col, "bold" if last else "normal", ha=aligns[ci])
            cx += widths[ci]
        ax.plot([x, x + tw], [y + rh, y + rh], color=GRID, lw=0.4, zorder=2)
        y += rh
    return y


def table_height(widths, head, rows, **kw):
    """Tính đúng chiều cao bảng mà không dựng một Figure matplotlib tạm."""
    size = kw.get("size", 7)
    row_h = kw.get("row_h", 6.2)
    wrap_cols = set(kw.get("wrap_cols", ()))
    lh = size * 0.3528 * 1.45
    total = row_h
    for row in rows:
        counts = []
        for ci, c in enumerate(row):
            counts.append(len(wrap(str(c), widths[ci] - 4, size)) if ci in wrap_cols else 1)
        total += max(row_h, max(counts or [1]) * lh + 2.4)
    return total


def gauge(ax, cx, cy, r, score):
    for a, b, c in [(0, 40, RED), (40, 70, AMBER), (70, 100, GREEN)]:
        ax.add_patch(Wedge((cx, cy), r, -(180 - a * 1.8), -(180 - b * 1.8), width=r * 0.26,
                           fc=c, ec="white", lw=1.2, zorder=3))
    txt(ax, cx - r + r * 0.13, cy + 3, "0", 6, MUTED, ha="center")
    txt(ax, cx + r - r * 0.13, cy + 3, "100", 6, MUTED, ha="center")
    if score is None:
        txt(ax, cx, cy + 5, "N/A", 22, MUTED, "bold", ha="center")
        return
    ang = math.radians(180 - score * 1.8)
    ax.plot([cx, cx + math.cos(ang) * r * 0.78], [cy, cy - math.sin(ang) * r * 0.78],
            color=INK, lw=2.2, zorder=6, solid_capstyle="round")
    ax.add_patch(Circle((cx, cy), 2.4, fc=INK, ec="white", lw=1, zorder=7))
    txt(ax, cx - 3, cy + 5, f"{score:.1f}", 24, INK, "bold", ha="center")
    txt(ax, cx + 19, cy + 12, "/100", 8, MUTED, ha="left")


def pill(ax, x, y, w, h, s, fc, color="white", size=7.5):
    box(ax, x, y, w, h, fc=fc, ec=fc, r=h / 2 - 0.1, z=3)
    txt(ax, x + w / 2, y + h / 2, s, size, color, "bold", ha="center", va="center")


# ======================= Chuyển `result` -> dữ liệu hiển thị =======================
def _signal_key(label: str) -> str:
    u = (label or "").upper()
    if "BUY" in u or "MUA" in u:
        return "BUY"
    if "SELL" in u or "BÁN" in u:
        return "SELL"
    return "HOLD"


def _view(result: dict, chart_img=None) -> dict:
    s = result.get("score") or {}
    tech = result.get("technical") or {}
    fund = result.get("fundamental") or {}
    sig = result.get("signal") or {}
    ci = result.get("company_info") or {}
    crit = fund.get("criteria") or {}
    comps = s.get("components") or {}
    comments = fund.get("comments") or {}
    method = result.get("methodology") or {}

    sig_label = sig.get("label") or "N/A"
    price = to_float(tech.get("close"))
    score = to_float(s.get("score"))
    F = (s.get("fundamental") or {}).get("score")
    T = (s.get("technical") or {}).get("score")
    sec = result.get("industry") or fund.get("company_type_label") or "N/A"

    # --- lịch sử năm ---
    years = []
    for r in reversed(fund.get("history") or []):
        years.append({
            "year": r.get("year", ""),
            "rev": _ty_num(r.get("revenue")), "rev_txt": _ty(r.get("revenue")),
            "rev_yoy": _fmt(r.get("revenue_growth"), "%"),
            "np": _ty_num(r.get("npat")), "np_txt": _ty(r.get("npat")),
            "np_yoy": _fmt(r.get("profit_growth"), "%"),
            "roe": _fmt(r.get("roe"), "%"), "roa": _fmt(r.get("roa"), "%"),
            "de": "N/A" if fund.get("company_type") != "regular" else _fmt(r.get("de"), "x"),
        })

    def crow(key):
        it = crit.get(key)
        return [it.get("name") or key, _fmt(it.get("value"), it.get("unit"))] if it else None

    health = [r for r in (crow(k) for k in ("revenue_growth", "profit_growth", "roe", "roa", "de")) if r]
    valuation = [r for r in (crow(k) for k in ("pe", "pb")) if r]

    fa_text = []
    for head, key in (("Tăng trưởng", "growth"), ("Khả năng sinh lời và đòn bẩy", "profitability_leverage"),
                      ("Định giá", "valuation")):
        items = comments.get(key) or []
        if items:
            fa_text.append((head, items))

    # --- bảng chỉ báo kỹ thuật ---
    tech_rows = [["Giá đóng cửa", _fmt(tech.get("close"))]]
    for label, keys in (("MA20", ("ma20", "MA20", "sma20")), ("MA50", ("ma50", "MA50", "sma50")),
                        ("MA150", ("ma150", "MA150")), ("RSI14", ("rsi14", "rsi", "RSI14"))):
        if any(str(it.get("name") or "").upper().startswith(label) for kk, it in comps.items() if kk not in FUND_KEYS):
            continue
        for k in keys:
            if to_float(tech.get(k)) is not None:
                tech_rows.append([label, _fmt(tech.get(k))])
                break
    for key, it in comps.items():
        if key in FUND_KEYS:
            continue
        tech_rows.append([it.get("name") or key,
                          "N/A" if it.get("points") is None else
                          f"{_fmt(it.get('value'), it.get('unit'))}  ({it['points']:.1f}/{to_float(it.get('weight'), 0):.0f})"])
    tech_rows.append(["Điểm kỹ thuật", "N/A" if T is None else f"{to_float(T):.1f}/100"])

    tech_text = (tech.get("comment") or tech.get("summary") or result.get("technical_comment") or "")
    if not tech_text:
        pool = [r for r in (result.get("risks") or []) + (result.get("weaknesses") or [])
                if any(k in str(r) for k in ("kỹ thuật", "RSI", "MA", "khối lượng"))]
        tech_text = " ".join(pool) or "Xem biểu đồ và bảng chỉ báo để đánh giá xu hướng; tín hiệu kỹ thuật được tính từ cùng dữ liệu giá với dashboard."

    news = [
        {
            "date": str(n.get("publish_time") or ""),
            "title": str(n.get("title") or ""),
            "source": str(n.get("source") or n.get("provider") or ""),
            "summary": str(n.get("summary") or ""),
            "sentiment": str(n.get("sentiment") or ""),
            "sentiment_reason": str(n.get("sentiment_reason") or ""),
            "url": str(n.get("url") or ""),
        }
        for n in (result.get("news") or []) if isinstance(n, dict)
    ]

    components = []
    gsum = {}
    for key, it in comps.items():
        wt = to_float(it.get("weight"), 0) or 0
        pts = to_float(it.get("points"))
        components.append({"name": it.get("name") or key, "value": _fmt(it.get("value"), it.get("unit")),
                           "max": wt, "got": pts if pts is not None else 0.0, "na": pts is None})
        g = GROUP_OF.get(key, "Kỹ thuật")
        gsum[g] = gsum.get(g, 0) + wt

    sources = []
    srcs = result.get("data_sources") or []
    got_set = {str(x.get("retrieved_at") or "") for x in srcs} - {""}
    same_time = next(iter(got_set)) if len(got_set) == 1 else ""
    for x in srcs:
        per = str(x.get("period") or "")
        got = str(x.get("retrieved_at") or "")
        if got and not same_time:
            per += f" · lấy {got}"
        sources.append([str(x.get("data") or ""), str(x.get("source") or ""), per, str(x.get("note") or "")])

    rules = [f"{k.upper()}: {method.get(k + '_rule', '')}" for k in ("buy", "hold", "sell") if method.get(k + "_rule")]
    overview_text = str(result.get("assessment") or "")
    overview_text = re.sub(
        r"\s*\(\s*cơ bản\s*[\d.,]+\s*/\s*100\s*;\s*kỹ thuật\s*[\d.,]+\s*/\s*100\s*\)\s*\.?",
        "", overview_text, flags=re.IGNORECASE,
    ).strip()
    reason = sig.get("reason") or ""
    mc = to_float(ci.get("market_cap"))

    return {
        "ticker": result.get("symbol", ""), "name": result.get("company_name") or "Doanh nghiệp niêm yết",
        "exchange": result.get("exchange") or ci.get("exchange") or "N/A", "sector": sec,
        "listing_date": ci.get("listing_date") or "N/A", "website": ci.get("website") or "N/A",
        "date": str(result.get("updated_at") or ""),
        "price_txt": "N/A" if price is None else f"{price:,.0f}",
        "mcap_txt": "N/A" if mc is None else f"{_ty(mc)} tỷ đồng",
        "score": score, "F": F, "T": T, "coverage": to_float(s.get("coverage_pct"), 0) or 0.0,
        "signal_key": _signal_key(sig_label), "signal_label": sig_label,
        "overview": overview_text,
        "strengths": result.get("investment_thesis") or result.get("strengths") or [],
        "strengths_all": result.get("strengths") or [],
        "weaknesses": result.get("weaknesses") or [], "risks": result.get("risks") or [],
        "formula": s.get("formula") or "S = 0.6 × F + 0.4 × T",
        "years": years, "health": health, "valuation": valuation, "fa_text": fa_text,
        "tech_table": tech_rows, "tech_text": tech_text, "news": news,
        "components": components,
        "groups": [(k, v) for k, v in gsum.items() if v > 0],
        "conclusion": f"{sig_label}. {reason}".strip(),
        "rules": rules, "sources": sources, "sources_time": same_time,
        "missing": ", ".join(result.get("missing_data") or []) or "Không có mục chấm điểm bị thiếu tại thời điểm tạo báo cáo.",
        "disclaimer": result.get("disclaimer") or "Kết quả phục vụ phân tích học thuật/tham khảo, không phải khuyến nghị đầu tư cá nhân hóa.",
        "ar_note": (result.get("annual_report_source") or {}).get("note") or "",
        "profile": ci.get("profile") or "",
        "chart_img": chart_img,
    }


# ======================= TRANG 1 =======================
def page1(d):
    NAVY, BLUE, LIGHT = d["brand"]["primary"], d["brand"]["secondary"], d["brand"]["light"]
    fig, ax = new_page()
    header(ax, None, "", d["ticker"], d["date"], d["brand"])
    scol, sbg = SIGNAL_COLORS[d["signal_key"]]

    box(ax, 10, 20, 190, 34)
    box(ax, 15, 26, 30, 22, fc=BLUE, ec=BLUE, r=3, z=2)
    txt(ax, 30, 37, d["ticker"], 17, "white", "bold", ha="center", va="center")
    txt(ax, 52, 27, d["name"], 14, NAVY, "bold")
    txt(ax, 52, 36, f"{d['exchange']}  ·  {d['sector']}", 8, MUTED)
    txt(ax, 52, 42.5, f"Niêm yết: {d['listing_date']}   ·   {d['website']}", 7, MUTED)

    for i, (a, b) in enumerate([("MÃ CỔ PHIẾU", d["ticker"]), ("GIÁ GẦN NHẤT (VND)", d["price_txt"]),
                                ("VỐN HÓA ƯỚC TÍNH", d["mcap_txt"])]):
        x = 10 + i * 64
        box(ax, x, 58, 62, 18, fc=LIGHT, ec=LIGHT)
        txt(ax, x + 4, 62, a, 6.2, MUTED, "bold")
        txt(ax, x + 4, 68, b, 12, NAVY, "bold")

    box(ax, 10, 82, 92, 72)
    card_title(ax, 15, 86, "ĐIỂM ĐẦU TƯ TỔNG HỢP", color=NAVY)
    gauge(ax, 56, 126, 30, d["score"])
    pill(ax, 17, 142, 78, 11, d["signal_label"], scol, size=12)

    box(ax, 106, 82, 94, 44)
    card_title(ax, 111, 86, "ĐÁNH GIÁ TỔNG QUAN", color=NAVY)
    para(ax, 111, 93, d["overview"], 84, size=10, max_lines=8)

    box(ax, 106, 130, 94, 24, fc="white", ec=GRID)
    card_title(ax, 111, 134, "THÔNG TIN DOANH NGHIỆP", color=NAVY, size=10)
    company_text = str(d.get("profile") or "").strip()
    if not company_text:
        company_text = f"Ngành: {d['sector']}. Sàn: {d['exchange']}. Website: {d['website']}."
    para(ax, 111, 140, company_text, 84, size=8.5, max_lines=3)

    box(ax, 10, 160, 92, 40, fc=GREEN_BG, ec=GREEN_BG)
    txt(ax, 15, 164, "LUẬN ĐIỂM ĐẦU TƯ", 11, GREEN, "bold")
    bullets(ax, 14, 172, d["strengths"][:4], 84, size=9.5, dot=GREEN, gap=1.5, max_y=198)
    box(ax, 108, 160, 92, 40, fc=RED_BG, ec=RED_BG)
    txt(ax, 113, 164, "RỦI RO / ĐIỂM CẦN THEO DÕI", 10, RED, "bold")
    bullets(ax, 112, 172, (d["risks"] or d["weaknesses"])[:4], 84, size=9.5, dot=RED, gap=1.5, max_y=198)

    box(ax, 10, 206, 190, 24)
    fT = "N/A" if d["F"] is None else f"{to_float(d['F']):.1f}/100"
    tT = "N/A" if d["T"] is None else f"{to_float(d['T']):.1f}/100"
    for i, (a, b, c) in enumerate([("ĐIỂM CƠ BẢN (F)", fT, "Trọng số 60%"), ("ĐIỂM KỸ THUẬT (T)", tT, "Trọng số 40%"),
                                   ("ĐỘ PHỦ DỮ LIỆU", f"{d['coverage']:.1f}%", "Tỷ lệ chỉ tiêu có dữ liệu")]):
        x = 14 + i * 62
        txt(ax, x, 210, a, 6.2, MUTED, "bold")
        txt(ax, x, 215.5, b, 13, NAVY, "bold")
        txt(ax, x, 224, c, 6.2, MUTED)

    for i, (a, b, c) in enumerate([("Phân tích cơ bản", "Trang 2", BLUE), ("Phân tích kỹ thuật", "Trang 3", GREEN),
                                   ("Tin tức doanh nghiệp", "Trang " + str(5 + int(d.get("_overflow", False))) if d["news"] else "Trang 3", AMBER), ("Chấm điểm & kết luận", "Trang 4", PURPLE)]):
        x = 10 + i * 48
        box(ax, x, 236, 46, 18)
        ax.add_patch(Circle((x + 7, 245), 3.6, fc=c, ec="none", zorder=3))
        txt(ax, x + 7, 245, str(i + 1), 7.5, "white", "bold", ha="center", va="center")
        txt(ax, x + 13, 241.5, a, 7, INK, "bold")
        txt(ax, x + 13, 247, b, 6.2, MUTED)
    txt(ax, 10, 259, f"Công thức: {d['formula']}", 6.4, MUTED)
    footer(ax, d, 1)
    return fig


# ======================= TRANG 2 =======================
def page2(d):
    NAVY, BLUE, LIGHT = d["brand"]["primary"], d["brand"]["secondary"], d["brand"]["light"]
    def themed_table(*args, **kwargs):
        kwargs.setdefault("head_fc", NAVY)
        return table(*args, **kwargs)
    fig, ax = new_page()
    header(ax, 2, "PHÂN TÍCH CƠ BẢN", d["ticker"], d["date"], d["brand"])

    card_title(ax, 10, 20, "2.1. Kết quả kinh doanh (tỷ đồng)", color=NAVY)
    rows = [[str(y["year"]), y["rev_txt"], y["rev_yoy"], y["np_txt"], y["np_yoy"], y["roe"], y["roa"], y["de"]]
            for y in d["years"]] or [["N/A", "Chưa đủ dữ liệu", "", "", "", "", "", ""]]
    yb = themed_table(ax, 10, 26, [14, 24, 14, 22, 14, 14, 14, 14],
               ["Năm", "Doanh thu", "YoY", "LNST", "YoY", "ROE", "ROA", "D/E"], rows, size=6.8, row_h=6.2,
               aligns=["left"] + ["right"] * 7)
    txt(ax, 10, yb + 2, "Năm chỉ được tổng hợp khi đủ 4 quý; giá trị thiếu hiển thị N/A, không thay bằng 0.", 5.8, MUTED)

    box(ax, 150, 18, 50, 52)
    txt(ax, 154, 20.5, "Doanh thu & LNST (tỷ đồng)", 6.8, NAVY, "bold")
    if d["years"]:
        cax = sub_axes(fig, 153, 28, 45, 38)
        ys = list(reversed(d["years"]))
        xs = range(len(ys))
        cax.bar([i - 0.2 for i in xs], [y["rev"] for y in ys], 0.4, color=BLUE, label="Doanh thu")
        cax.bar([i + 0.2 for i in xs], [y["np"] for y in ys], 0.4, color=GREEN, label="LNST")
        cax.set_xticks(list(xs))
        cax.set_xticklabels([str(y["year"]) for y in ys], fontsize=5.5)
        cax.tick_params(axis="y", labelsize=5, length=0)
        cax.tick_params(axis="x", length=0)
        cax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, p: f"{v/1000:.0f}k"))
        for sp in ("top", "right", "left"):
            cax.spines[sp].set_visible(False)
        cax.spines["bottom"].set_color(GRID)
        cax.grid(axis="y", color=GRID, lw=0.4)
        cax.set_axisbelow(True)
        cax.legend(fontsize=5, frameon=False, loc="upper left")

    y0 = max(yb + 8, 78)
    card_title(ax, 10, y0, "2.2. Sức khỏe tài chính", color=NAVY)
    yh = themed_table(ax, 10, y0 + 6, [48, 30], ["Chỉ tiêu", "Giá trị"], d["health"] or [["N/A", ""]], size=7, aligns=["left", "right"], head_fc=NAVY)
    card_title(ax, 108, y0, "2.3. Định giá", color=NAVY)
    yv = themed_table(ax, 108, y0 + 6, [48, 44], ["Chỉ tiêu", "Giá trị"], d["valuation"] or [["N/A", ""]], size=7, aligns=["left", "right"], head_fc=NAVY)

    yt = max(yh, yv) + 7
    card_title(ax, 10, yt, "2.4. Nhận định phân tích cơ bản", color=NAVY)
    yt += 6
    for head, items in d["fa_text"]:
        txt(ax, 10, yt, head, 9, BLUE, "bold")
        yt = bullets(ax, 10, yt + 4.8, items, 190, size=8.5, dot=BLUE, gap=0.6, max_y=270)

    by = yt + 3
    bh = min(48, 281 - by)
    if bh >= 28:
        for i, (t, c, bg, items) in enumerate([("ĐIỂM MẠNH", GREEN, GREEN_BG, d["strengths_all"] or d["strengths"]),
                                               ("ĐIỂM YẾU", AMBER, AMBER_BG, d["weaknesses"]),
                                               ("RỦI RO", RED, RED_BG, d["risks"])]):
            x = 10 + i * 64
            box(ax, x, by, 62, bh, fc=bg, ec=bg)
            txt(ax, x + 4, by + 4, t, 8, c, "bold")
            bullets(ax, x + 3, by + 11, items[:4], 56, size=8.5, dot=c, gap=0.8, max_y=by + bh - 2)
    footer(ax, d, 2)
    return fig


# ======================= TRANG 3 =======================
def page3(d):
    NAVY, BLUE, LIGHT = d["brand"]["primary"], d["brand"]["secondary"], d["brand"]["light"]
    def themed_table(*args, **kwargs):
        kwargs.setdefault("head_fc", NAVY)
        return table(*args, **kwargs)
    fig, ax = new_page()
    header(ax, 3, "PHÂN TÍCH KỸ THUẬT VÀ TIN TỨC THỊ TRƯỜNG", d["ticker"], d["date"], d["brand"])
    card_title(ax, 10, 20, "3.1. Phân tích kỹ thuật", color=NAVY)
    box(ax, 10, 26, 190, 112, fc="white", ec=GRID)
    if d["chart_img"] is not None:
        iax = sub_axes(fig, 12, 28, 186, 108)
        iax.imshow(d["chart_img"], aspect="auto")
        iax.axis("off")
    else:
        txt(ax, 105, 82, "Chưa có biểu đồ giá", 9, INK, ha="center", va="center")

    yb = 144
    card_title(ax, 10, yb, "Các chỉ báo kỹ thuật", color=NAVY)
    yt = themed_table(ax, 10, yb + 6, [38, 44], ["Chỉ báo", "Giá trị"], d["tech_table"], size=7, aligns=["left", "right"], head_fc=NAVY)
    box(ax, 98, yb + 6, 102, max(yt - yb - 6, 30), fc=LIGHT, ec=LIGHT)
    txt(ax, 102, yb + 9, "Nhận định kỹ thuật", 7.8, NAVY, "bold")
    para(ax, 102, yb + 15, d["tech_text"], 94, size=7, max_lines=10)

    yn = yt + 8
    card_title(ax, 10, yn, "3.2. Phân tích tin tức thị trường", color=NAVY)
    rows = [[n["date"], n["title"], n["source"]] for n in d["news"][:6]] or [["", "Chưa lấy được tin tức từ nguồn hiện có.", ""]]
    ye = themed_table(ax, 10, yn + 6, [30, 128, 32], ["Ngày", "Tiêu đề", "Nguồn"], rows, size=6.8, row_h=7,
               wrap_cols=(1,))
    para(ax, 10, ye + 3, "Tin tức chỉ dùng làm bối cảnh tham khảo, chưa đưa trực tiếp vào điểm BUY/HOLD/SELL.",
         190, size=6, color=MUTED)
    footer(ax, d, 3)
    return fig


# ======================= TRANG 4 (+5) =======================
SRC_W = [36, 40, 34, 80]
SRC_HEAD = ["Dữ liệu", "Nguồn", "Kỳ dữ liệu", "Ghi chú"]


def _sources_block(ax, d, y):
    card_title(ax, 10, y, "4.3. Nguồn dữ liệu và khả năng kiểm chứng")
    if d.get("sources_time"):
        txt(ax, W - 10, y + 1, f"Thời điểm lấy dữ liệu: {d['sources_time']}", 6.2, MUTED, ha="right")
    rows = d["sources"] or [["N/A", "", "", ""]]
    y3 = table(ax, 10, y + 6, SRC_W, SRC_HEAD, rows, size=5.9, row_h=5.6, wrap_cols=(0, 1, 2, 3))
    txt(ax, 10, y3 + 3, "Dữ liệu thiếu: " + d["missing"], 6.2, MUTED)
    if d["ar_note"]:
        y3 = para(ax, 10, y3 + 8, "vn-annual-report-miner: " + d["ar_note"], 190, size=6, color=MUTED, max_lines=2) - 3
    txt(ax, 10, y3 + 8, d["disclaimer"], 6.2, MUTED, style="italic")
    return y3 + 12


def page4(d):
    NAVY, BLUE, LIGHT = d["brand"]["primary"], d["brand"]["secondary"], d["brand"]["light"]
    def themed_table(*args, **kwargs):
        kwargs.setdefault("head_fc", NAVY)
        return table(*args, **kwargs)
    fig, ax = new_page()
    header(ax, 4, "CHẤM ĐIỂM ĐẦU TƯ TỔNG HỢP", d["ticker"], d["date"], d["brand"])
    scol, sbg = SIGNAL_COLORS[d["signal_key"]]

    card_title(ax, 10, 20, "4.1. Bảng điểm thành phần", color=NAVY)
    rows, colors = [], {}
    for i, c in enumerate(d["components"]):
        rows.append([c["name"], c["value"], f"{c['max']:g}", "N/A" if c["na"] else f"{c['got']:.1f}"])
        ratio = c["got"] / c["max"] if c["max"] else 0
        colors[(i, 3)] = MUTED if c["na"] else (GREEN if ratio >= 0.7 else (AMBER if ratio >= 0.4 else RED))
    tmax = sum(c["max"] for c in d["components"])
    tgot = sum(c["got"] for c in d["components"])
    rows.append(["Tổng điểm", "", f"{tmax:g}", f"{tgot:.1f}"])
    yb = themed_table(ax, 10, 26, [52, 26, 24, 24], ["Tiêu chí", "Giá trị", "Điểm tối đa", "Điểm đạt"], rows,
               size=7, row_h=6.4, aligns=["left", "right", "right", "right"], bold_last=True, colors=colors)

    box(ax, 150, 20, 50, 62)
    txt(ax, 154, 23, "Cơ cấu điểm tối đa", 7, NAVY, "bold")
    groups = d["groups"]
    tot = sum(v for _, v in groups) or 1
    pal = [BLUE, GREEN, AMBER, PURPLE, RED]
    start, cx, cy, r = 90, 175, 46, 15
    for i, (nm, v) in enumerate(groups):
        ext = 360 * v / tot
        ax.add_patch(Wedge((cx, cy), r, -start, -start + ext, width=r * 0.42, fc=pal[i % 5], ec="white", lw=1, zorder=3))
        start -= ext
    txt(ax, cx, cy, "100", 11, NAVY, "bold", ha="center", va="center")
    ly = 64
    for i, (nm, v) in enumerate(groups):
        ax.add_patch(Rectangle((154, ly + 0.6), 2.6, 2.6, fc=pal[i % 5], ec="none", zorder=4))
        txt(ax, 158.5, ly, f"{nm}  {v / tot * 100:.0f}%", 6.2, INK)
        ly += 3.7

    y2 = max(yb, 84) + 8
    card_title(ax, 10, y2, "4.2. Kết luận và đánh giá cơ hội đầu tư", color=NAVY)
    y2 += 6
    bh = 29
    for i, (t, c, bg, items) in enumerate([("ĐIỂM MẠNH NỔI BẬT", GREEN, GREEN_BG, d["strengths_all"] or d["strengths"]),
                                           ("ĐIỂM YẾU / LƯU Ý", AMBER, AMBER_BG, d["weaknesses"]),
                                           ("RỦI RO CHÍNH", RED, RED_BG, d["risks"])]):
        x = 10 + i * 64
        box(ax, x, y2, 62, bh, fc=bg, ec=bg)
        txt(ax, x + 4, y2 + 3.5, t, 7.5, c, "bold")
        bullets(ax, x + 3, y2 + 9, items[:3], 56, size=8, dot=c, gap=0.6, max_y=y2 + bh - 2)
    y2 += bh + 4
    box(ax, 10, y2, 190, 22, fc=sbg, ec=sbg)
    txt(ax, 14, y2 + 3.5, "NHẬN ĐỊNH TỔNG HỢP", 7.5, scol, "bold")
    pill(ax, 150, y2 + 2.5, 46, 6.5, d["signal_label"], scol, size=6.2)
    para(ax, 14, y2 + 10.5, d["conclusion"], 182, size=8, max_lines=3)
    y2 += 22 + 3
    for rule in d["rules"]:
        y2 = para(ax, 10, y2, rule, 190, size=5.9, color=MUTED, lead=1.4, max_lines=2)
    y2 += 4

    need = 4 + table_height(SRC_W, SRC_HEAD, d["sources"] or [["N/A", "", "", ""]], size=5.9, row_h=5.6,
                            wrap_cols=(0, 1, 2, 3)) + 20
    if y2 + need <= 284:
        _sources_block(ax, d, y2)
    footer(ax, d, 4)
    return fig, (y2 + need > 284)


def page5(d):
    NAVY, BLUE, LIGHT = d["brand"]["primary"], d["brand"]["secondary"], d["brand"]["light"]
    fig, ax = new_page()
    header(ax, 5, "NGUỒN DỮ LIỆU VÀ KHẢ NĂNG KIỂM CHỨNG", d["ticker"], d["date"], d["brand"])
    _sources_block(ax, d, 22)
    footer(ax, d, 5)
    return fig





def _news_page_specs(d):
    """Gộp các bản tin trên cùng trang, tự chuyển trang khi hết chỗ."""
    specs = []
    current_page = []
    used_height = 0.0

    # Vùng nội dung: từ y=24 đến y=280 mm.
    max_height = 250.0
    line_factor = 0.3528 * 1.55

    def add_line(content, size=8.5, kind="normal"):
        nonlocal current_page, used_height

        line_height = size * line_factor
        if kind == "separator":
            line_height = 6.0

        if current_page and used_height + line_height > max_height:
            specs.append(current_page)
            current_page = []
            used_height = 0.0

        current_page.append((content, size, kind))
        used_height += line_height

    def add_text(content, size=8.5, kind="normal"):
        for paragraph in str(content or "").splitlines() or [""]:
            for line in wrap(paragraph, 180, size):
                add_line(line, size, kind)

    for index, n in enumerate(d["news"][:3], 1):
        # Chừa đủ chỗ cho phần mở đầu bài tin.
        if current_page and used_height + 32 > max_height:
            specs.append(current_page)
            current_page = []
            used_height = 0.0

        if current_page:
            add_line("", 8, "separator")

        add_line(f"BÀI TIN {index} — {d['ticker']}", 10, "article")
        add_text(n["title"] or "Không có tiêu đề", 10, "title")

        metadata = " | ".join(
            x for x in (n["date"], n["source"]) if x
        )
        add_text(metadata or "Không có thông tin", 7.5, "meta")

        add_line("", 5, "space")
        add_line("TÓM TẮT NỘI DUNG", 8.5, "heading")

        summary = n["summary"] or "Chưa có tóm tắt từ nguồn dữ liệu."
        if "GROQ_API_KEY" in summary or "Chưa cấu hình" in summary:
            summary = "Chưa có tóm tắt tin tức."

        add_text(summary, 8.5, "normal")

        add_line("", 5, "space")
        add_line("LIÊN KẾT BÀI VIẾT", 8, "heading")
        add_text(n["url"] or "Không có liên kết bài viết", 7, "url")

        add_line("", 6, "space")

    if current_page:
        specs.append(current_page)

    return specs



def page_news(d, spec, page_no):
    fig, ax = new_page()

    header(
        ax, page_no, "TIN TỨC DOANH NGHIỆP",
        d["ticker"], d["date"], d["brand"]
    )

    y = 24.0
    line_factor = 0.3528 * 1.55

    for content, size, kind in spec:
        if kind == "separator":
            ax.plot(
                [12, 198], [y + 2, y + 2],
                color=GRID, lw=0.9
            )
            y += 6.0
            continue

        color = INK
        weight = "normal"

        if kind == "article":
            color = NAVY
            weight = "bold"
        elif kind == "title":
            color = NAVY
            weight = "bold"
        elif kind == "heading":
            color = BLUE
            weight = "bold"
        elif kind == "meta":
            color = MUTED
        elif kind == "url":
            color = BLUE

        if content:
            txt(
                ax, 12, y, content,
                size=size,
                color=color,
                weight=weight
            )

        y += size * line_factor

    footer(ax, d, page_no)
    return fig




# Trang bìa cố định theo thiết kế Nhóm 1. Ảnh đặt cùng thư mục với file Python.
COVER_IMAGE = Path(__file__).resolve().parent / 'report_cover.jpg'
_COVER_ARRAY = None
_COVER_LOCK = threading.Lock()
_CHART_CACHE: dict[str, tuple[float, object]] = {}
_CHART_CACHE_LOCK = threading.Lock()
_CHART_CACHE_TTL = 300.0

def page_cover():
    if not COVER_IMAGE.is_file():
        raise FileNotFoundError(
            f'Thiếu ảnh bìa {COVER_IMAGE}. Hãy đặt report_cover.jpg cùng thư mục report_pdf_new.py.'
        )
    global _COVER_ARRAY
    if _COVER_ARRAY is None:
        with _COVER_LOCK:
            if _COVER_ARRAY is None:
                _COVER_ARRAY = mpimg.imread(str(COVER_IMAGE))
    fig, ax = new_page()
    ax.imshow(_COVER_ARRAY, extent=(0, W, H, 0), aspect='auto', interpolation='lanczos', zorder=1)
    ax.set_xlim(0, W)
    ax.set_ylim(H, 0)
    return fig


def _page4_overflow_needed(d) -> bool:
    rows = [[c["name"], c["value"], f"{c['max']:g}", "N/A" if c["na"] else f"{c['got']:.1f}"] for c in d["components"]]
    tmax = sum(c["max"] for c in d["components"])
    tgot = sum(c["got"] for c in d["components"])
    rows.append(["Tổng điểm", "", f"{tmax:g}", f"{tgot:.1f}"])
    yb = 26 + table_height([52,26,24,24], ["Tiêu chí","Giá trị","Điểm tối đa","Điểm đạt"], rows, size=7, row_h=6.4)
    y2 = max(yb, 84) + 8 + 6 + 29 + 4 + 22 + 3
    # rules() are capped to 2 wrapped lines with lead 1.4; estimate conservatively.
    for rule in d["rules"]:
        n = min(2, max(1, len(wrap(str(rule), 190, 5.9))))
        y2 += n * 5.9 * 0.3528 * 1.4
    y2 += 4
    need = 4 + table_height(SRC_W, SRC_HEAD, d["sources"] or [["N/A","","",""]], size=5.9, row_h=5.6, wrap_cols=(0,1,2,3)) + 20
    return y2 + need > 284


def _cached_chart_image(symbol: str):
    now=time.time(); key=symbol.upper()
    with _CHART_CACHE_LOCK:
        hit=_CHART_CACHE.get(key)
        if hit and now-hit[0] < _CHART_CACHE_TTL:
            return hit[1]
    frame, info = charts.build_chart_frame(key)
    png = charts.render_chart_png(frame, info, "6M")
    img = mpimg.imread(io.BytesIO(png), format="png")
    with _CHART_CACHE_LOCK:
        _CHART_CACHE[key]=(now,img)
    return img

def build_report_pdf(result: dict, *, include_chart: bool = True) -> bytes:
    chart_img = None
    if include_chart:
        try:
            chart_img = _cached_chart_image(result["symbol"])
        except Exception:
            import traceback
            traceback.print_exc()
            chart_img = None
    d = _view(result, chart_img)
    d["brand"] = {"primary": NAVY, "secondary": BLUE, "light": LIGHT, "logo": None}
    d["_total"] = 4
    overflow = _page4_overflow_needed(d)
    d["_overflow"] = overflow
    news_specs = _news_page_specs(d)
    d["_total"] = 4 + int(overflow) + len(news_specs)
    buffer = io.BytesIO()
    with PdfPages(buffer) as pdf:
        pages = [page_cover(), page1(d), page2(d), page3(d), page4(d)[0]]
        if overflow:
            pages.append(page5(d))
        for i, spec in enumerate(news_specs, start=len(pages)):
            pages.append(page_news(d, spec, i))
        for fig in pages:
            pdf.savefig(fig)
            plt.close(fig)
        info_d = pdf.infodict()
        info_d["Title"] = f"Báo cáo phân tích cơ hội đầu tư {d['ticker']}"
        info_d["Author"] = "Stock Opportunity Analyzer"
    return buffer.getvalue()