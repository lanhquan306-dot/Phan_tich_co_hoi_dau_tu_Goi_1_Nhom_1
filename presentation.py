"""Định dạng kết quả engine thành tin nhắn Telegram (HTML parse mode).

Bố cục chung của một thẻ:
    tiêu đề có icon  ->  giá + điểm + thị trường  ->  câu trả lời  ->  checklist BUY
    ->  khối "chi tiết" thu gọn (<blockquote expandable>, bấm để mở)  ->  nguồn dữ liệu.

Quy tắc HTML của Telegram cần nhớ: trong <blockquote> chỉ dùng được <b>/<i>/<u>/<s>;
bảng <pre> phải đặt ngoài blockquote. Mọi chữ lấy từ dữ liệu/AI đều được html.escape.
"""
from __future__ import annotations

import html
import re

import pandas as pd

import stock_service as service
from config import STRATEGY
from conversation import FALLBACK_TEXT, quote_summary, score_text, tier_score_text
from utils import fmt_money, fmt_num, fmt_pct, fmt_price, fmt_volume, to_float

DISCLAIMER = "<i>ℹ️ Tham khảo cho mục đích học tập, không phải tư vấn đầu tư.</i>"
DIVIDER = "━━━━━━━━━━━━━━━━━━"

SIGNAL_ICONS = {"BUY": "🟢", "HOLD": "🟡", "SELL": "🔴"}
MARKET_ICONS = {"UPTREND": "🟢", "PRESSURE": "🟠", "WEAK": "🟡", "DOWNTREND": "🔴"}
MARKET_HINTS = {
    "UPTREND": "được mở mua mới",
    "PRESSURE": "mua thận trọng, giảm tỷ trọng",
    "WEAK": "tạm dừng mua mới",
    "DOWNTREND": "đứng ngoài, ưu tiên bảo vệ vốn",
}
MA_ICONS = {"ma20": "🟨", "ma50": "🟦", "ma150": "🟪"}   # trùng màu đường MA trên biểu đồ
TIER_TITLES = {"tier1": ("🧱", "T1 Cơ bản", 30), "tier2": ("📈", "T2 Xu hướng", 30), "tier3": ("⚡", "T3 Breakout", 40)}
BUY_CHECKS = (
    ("market", "Thị trường"), ("hard_filter", "Bộ lọc"), ("fundamental_source", "BCTC"),
    ("mandatory_breakout", "Breakout"), ("score", "Đủ điểm"), ("no_position", "Chưa giữ"),
)
MEDALS = ("🥇", "🥈", "🥉")


# =====================================================================
# Khối dựng nhỏ
# =====================================================================
def _esc(value) -> str:
    return html.escape(str(value if value not in (None, "") else "N/A"))


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _ai_block(text: str, limit: int) -> str:
    """Escape phần chữ do AI/planner viết và cắt độ dài."""
    text = (text or "").strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return html.escape(text)


def _expandable(lines: list[str]) -> list[str]:
    """Khối thu gọn: Telegram chỉ hiện vài dòng đầu, người dùng bấm để mở."""
    return ["<blockquote expandable>" + "\n".join(lines) + "</blockquote>"] if lines else []


def progress_bar(value, total: float, width: int = 10) -> str:
    """58/100 -> '▰▰▰▰▰▰▱▱▱▱'."""
    x = to_float(value)
    filled = 0 if x is None or total <= 0 else round(max(0.0, min(1.0, x / total)) * width)
    return "▰" * filled + "▱" * (width - filled)


def range_bar(low, high, price, width: int = 10) -> str:
    """Vị trí giá trong biên độ thấp–cao của phiên: '━━━━●━━━━━'."""
    lo, hi, p = to_float(low), to_float(high), to_float(price)
    if None in (lo, hi, p) or hi <= lo:
        return "━" * width
    pos = round((min(max(p, lo), hi) - lo) / (hi - lo) * (width - 1))
    return "━" * pos + "●" + "━" * (width - 1 - pos)


def _arrow(pct) -> str:
    x = to_float(pct)
    return "▪️" if not x else ("🔺" if x > 0 else "🔻")


def change_text(price, reference, pct) -> str:
    """'🔺 +1,200 (+1.23%)' từ giá, giá tham chiếu và % thay đổi."""
    p, ref, pc = to_float(price), to_float(reference), to_float(pct)
    if pc is None:
        return "▪️ N/A"
    amount = f"{p - ref:+,.0f} " if p is not None and ref else ""
    return f"{_arrow(pc)} {amount}({pc:+.2f}%)"


def _pct_from(base, level) -> str:
    """' (+2.4%)': mốc `level` cao/thấp hơn `base` bao nhiêu %."""
    b, lv = to_float(base), to_float(level)
    return "" if not b or lv is None else f" ({(lv / b - 1) * 100:+.1f}%)"


def rsi_label(rsi) -> str:
    x = to_float(rsi)
    if x is None:
        return "N/A"
    zone = "🔥 quá mua" if x >= 70 else "🧊 quá bán" if x <= 30 else "⚖️ trung tính"
    return f"{x:.1f} {zone}"


def market_badge(gate: dict) -> str:
    state = gate.get("state") or "UNKNOWN"
    return f"{MARKET_ICONS.get(state, '⚪')} <b>{_esc(state)}</b>"


def _weight_text(gate: dict) -> str:
    state = gate.get("state")
    if state == "UPTREND":
        return f"{STRATEGY.position_weight:.0%} NAV/mã"
    if state == "PRESSURE":
        return f"{STRATEGY.position_weight * gate.get('weight_factor', 0):.0%} NAV/mã (giảm một nửa)"
    return "0% — không mở vị thế mới"


def trend_label(tech: dict) -> str:
    """Đọc thứ tự Giá / MA20 / MA50 / MA150 thành nhận định xu hướng."""
    c, m20, m50, m150 = (to_float(tech.get(k)) for k in ("close", "ma20", "ma50", "ma150"))
    if None in (c, m50):
        return "❔ Chưa đủ dữ liệu MA"
    if None not in (m20, m150) and c > m20 > m50 > m150:
        text = "🚀 Tăng mạnh: Giá &gt; MA20 &gt; MA50 &gt; MA150"
    elif None not in (m20, m150) and c < m20 < m50 < m150:
        text = "📉 Giảm: Giá &lt; MA20 &lt; MA50 &lt; MA150"
    elif c > m50:
        text = "↗️ Trên MA50, các đường MA chưa xếp tăng đồng thuận"
    else:
        text = "↘️ Dưới MA50"
    return text + (" · MA50 dốc xuống" if tech.get("ma50_down") else "")


def _header(r: dict) -> list[str]:
    subtype = f" <i>({_esc(r['hold_type'])})</i>" if r.get("hold_type") else ""
    name = " · ".join(x for x in (r.get("company_name"), r.get("exchange")) if x and x != "UNKNOWN")
    icon = SIGNAL_ICONS.get(r.get("signal"), "⚪")
    lines = [f"{icon} <b>{_esc(r.get('symbol'))} · {_esc(r.get('signal'))}</b>{subtype}"]
    if name:
        lines.append(f"<i>{html.escape(name)}</i>")
    return lines


def _price_line(r: dict) -> str:
    q = r.get("quote") or {}
    gtgd = f" · 💵 {fmt_money(r['gtgd'])}" if r.get("gtgd") else ""
    return (f"💰 <b>{fmt_price(q.get('price'))}</b> "
            f"{change_text(q.get('price'), q.get('reference'), q.get('percent_change'))}{gtgd}")


def _score_line(r: dict) -> str:
    threshold = to_float(r.get("threshold"))
    base = to_float(r.get("score") if r.get("score_complete", True) else r.get("score_min"))
    if threshold is None:
        need = " · ⛔ chưa mở BUY"
    else:
        gap = max(0.0, threshold - (base or 0))
        need = f" · BUY ≥ {threshold:g}" + (f" (thiếu {gap:g})" if gap else " ✅")
    return f"🎯 Điểm <b>{score_text(r)}</b>/100 {progress_bar(base, 100)}{need}"


def _market_line(gate: dict) -> str:
    return f"🏛 Thị trường {market_badge(gate)} — {MARKET_HINTS.get(gate.get('state'), 'chưa xác định')}"


def buy_checklist(r: dict) -> list[str]:
    """6 điều kiện BUY dạng ✅/❌, 3 điều kiện mỗi dòng."""
    checks = r.get("checks") or {}
    if not checks:
        return []
    marks = [f"{'✅' if checks.get(key) else '❌'} {label}" for key, label in BUY_CHECKS]
    passed = sum(bool(checks.get(key)) for key, _ in BUY_CHECKS)
    return [f"📋 <b>Điều kiện BUY: {passed}/{len(BUY_CHECKS)}</b>", "  ".join(marks[:3]), "  ".join(marks[3:])]


def _tier_lines(r: dict, with_components: bool) -> list[str]:
    out = []
    for key, (icon, title, max_score) in TIER_TITLES.items():
        tier = r.get(key) or {}
        base = tier.get("score") if tier.get("score") is not None else tier.get("score_min")
        out.append(f"{icon} <b>{title}</b> {tier_score_text(r, key)}/{max_score} {progress_bar(base, max_score, 8)}")
        if with_components:
            for code, comp in (tier.get("components") or {}).items():
                points = "?" if comp.get("points") is None else comp["points"]
                out.append(f"   ▸ {code} {points}/{comp.get('max')} — {_esc(comp.get('detail'))}")
    return out


def _indicator_lines(r: dict) -> list[str]:
    tech, fund = r.get("tech") or {}, r.get("fund") or {}
    price = (r.get("quote") or {}).get("price") or tech.get("close")
    out = [f"🧭 <b>Xu hướng</b>: {trend_label(tech)}"]
    for k in ("ma20", "ma50", "ma150"):
        if tech.get(k) is not None:
            gap = _pct_from(tech.get(k), price).strip(" ()")
            out.append(f"   {MA_ICONS[k]} {k.upper()} {fmt_price(tech.get(k))}" + (f" · giá {gap}" if gap else ""))
    out.append(f"⚡ <b>Động lượng</b>: RSI14 {rsi_label(tech.get('rsi14'))} · "
               f"KL 3 phiên {fmt_num(tech.get('vol_ratio_3d'))}× nền 20")
    out.append(f"🏁 <b>Sức mạnh</b>: ROC63 {fmt_pct(tech.get('roc63'), signed=True)} vs VN-Index "
               f"{fmt_pct(tech.get('index_roc63'), signed=True)} → Δ {fmt_pct(tech.get('delta_roc63'), signed=True)}")
    debt = "miễn (tài chính)" if fund.get("is_financial") else fmt_num(fund.get("debt_equity"))
    out.append(f"🧾 <b>Cơ bản</b>: EPS YoY {fmt_pct(fund.get('eps_yoy'), signed=True)} · "
               f"ROE {fmt_pct(fund.get('roe'))} · Nợ vay/VCSH {debt}")
    return out


def price_map(r: dict) -> list[str]:
    """Các mốc giá quan trọng xếp từ cao xuống thấp, chèn giá hiện tại vào đúng vị trí."""
    tech = r.get("tech") or {}
    b = tech.get("breakout") or {}
    price = to_float((r.get("quote") or {}).get("price") or tech.get("close"))
    if price is None:
        return []
    candidates = [
        ("🏔 Đỉnh 52 tuần", tech.get("high52")),
        ("🎯 Vùng mua tối đa", b.get("buy_zone") if b.get("found") else None),
        ("🚪 Pivot breakout" if b.get("found") else "🚪 Đỉnh 30 phiên", b.get("pivot") or tech.get("current_pivot")),
        (f"{MA_ICONS['ma20']} MA20", tech.get("ma20")),
        (f"{MA_ICONS['ma50']} MA50", tech.get("ma50")),
        (f"{MA_ICONS['ma150']} MA150", tech.get("ma150")),
        ("🛟 Đáy 20 phiên", tech.get("low20")),
        ("🛑 Cắt lỗ tham chiếu", r.get("stop_ref")),
    ]
    levels, seen = [], set()
    for label, value in candidates:
        v = to_float(value)
        if v is not None and round(v) not in seen:      # bỏ mốc trùng giá (VD pivot = đỉnh 52 tuần)
            seen.add(round(v))
            levels.append((label, v, False))
    levels.append(("👉 <b>Giá hiện tại</b>", price, True))
    levels.sort(key=lambda item: -item[1])
    return [f"{label} {fmt_price(v)}" + ("" if current else _pct_from(price, v)) for label, v, current in levels]


def _data_footer(r: dict) -> str:
    fund = r.get("fund") or {}
    return (f"🕒 <i>EOD {_esc(r.get('confirmed_bar_date'))} · VN-Index {_esc(r.get('index_bar_date'))} · "
            f"BCTC {_esc(fund.get('latest_period'))} · nguồn {_esc(r.get('price_source'))}</i>")


def _warning_lines(items: list[str], limit: int) -> list[str]:
    return [f"⚠️ <i>{html.escape(_clip(x, 110))}</i>" for x in items[:limit]]


# =====================================================================
# Thẻ phân tích
# =====================================================================
def conversational_stock_card(r: dict, text: str) -> str:
    """Chat tự nhiên về một mã: tóm tắt ngắn phía trên, chi tiết đầy đủ trong khối thu gọn."""
    lines = [
        *_header(r), DIVIDER,
        _price_line(r), _score_line(r), _market_line(r.get("market") or {}), DIVIDER,
        f"💬 {_ai_block(text, 1400) or 'Engine đã có dữ liệu nhưng chưa tạo được phần diễn giải.'}",
        "",
        *buy_checklist(r),
        *_expandable([
            "🔎 <b>Chi tiết chấm điểm</b>",
            *_tier_lines(r, with_components=True),
            "",
            *_indicator_lines(r),
            "",
            "🗺 <b>Bản đồ mốc giá</b>",
            *price_map(r),
        ]),
        _data_footer(r),
        *_warning_lines(r.get("warnings") or [], 2),
        DISCLAIMER,
    ]
    return "\n".join(lines)


def signal_card(r: dict) -> str:
    """Lệnh /signal: toàn bộ chi tiết để kiểm tra, nhóm theo khối có icon."""
    tech, fund, gate = r.get("tech") or {}, r.get("fund") or {}, r.get("market") or {}
    lines = [
        *_header(r), DIVIDER,
        _price_line(r), _score_line(r),
        _market_line(gate), f"⚖️ Tỷ trọng khuyến nghị: {_weight_text(gate)}",
        DIVIDER,
        *buy_checklist(r),
    ]
    if r.get("signal") == "BUY":
        lines += [
            "",
            "🛒 <b>KẾ HOẠCH VÀO LỆNH (phiên kế tiếp)</b>",
            f"   Chỉ mua khi Open &gt; {fmt_price(r.get('breakout_pivot'))} và ≤ {fmt_price(r.get('buy_zone'))}",
            f"   Tỷ trọng {fmt_pct((r.get('weight') or 0) * 100, digits=0)} NAV · "
            f"🛑 cắt lỗ {fmt_price(r.get('stop_ref'))}{_pct_from(tech.get('close'), r.get('stop_ref'))}",
        ]
    lines += ["", "📊 <b>ĐIỂM TỪNG TẦNG</b>", *_tier_lines(r, with_components=False)]
    lines += _expandable(["🔎 <b>Thành phần điểm</b>", *_tier_lines(r, with_components=True)])
    lines += ["", *_indicator_lines(r)]

    hard = r.get("hard") or {"checks": {}}
    lines += ["", f"🛡 <b>Bộ lọc cứng: {'✅ PASS' if hard.get('pass') else '❌ FAIL'}</b>"]
    lines += [f"   {'✅' if v['ok'] else '❌'} {_esc(v['detail'])}" for v in hard["checks"].values()]
    lines += _expandable(["🗺 <b>Bản đồ mốc giá</b>", *price_map(r)])
    if r.get("reasons"):
        lines += ["", "📝 <b>Lý do</b>", *[f"• {_esc(x)}" for x in r["reasons"][:6]]]
    if r.get("warnings") or r.get("errors"):
        lines += _expandable(["⚠️ <b>Cảnh báo dữ liệu</b>",
                              *[f"• {_esc(x)}" for x in (r.get("warnings") or [])[:6]],
                              *[f"❗ {_esc(x)}" for x in (r.get("errors") or [])[:3]]])
    lines += [_data_footer(r) + f" <i>· BCTC từ {_esc(fund.get('fund_source'))}</i>", DISCLAIMER]
    return "\n".join(lines)


def _rank_key(r: dict):
    rank = {"BUY": 3, "HOLD": 2, "SELL": 1}
    delta = to_float((r.get("tech") or {}).get("delta_roc63"))
    return rank.get(r.get("signal"), 0), to_float(r.get("score_min")) or 0, -1e9 if delta is None else delta


def _pct_cell(value) -> str:
    x = to_float(value)
    return "–" if x is None else f"{x:+.1f}%"


def comparison_card(results: list[dict], text: str) -> str:
    """So sánh nhiều mã: kết luận + dòng tóm tắt từng mã + bảng đối chiếu thẳng hàng (<pre>)."""
    if not results:
        return "Không có dữ liệu để so sánh."
    gate = results[0].get("market") or {}
    best = max(results, key=_rank_key)
    width = max(9, *(len(str(r.get("symbol"))) + 2 for r in results))

    def row(label: str, values) -> str:
        return label.ljust(10) + "".join(str(v)[:width].rjust(width) for v in values)

    def vs_ma50(r: dict) -> str:
        t = r.get("tech") or {}
        c, m = to_float(t.get("close")), to_float(t.get("ma50"))
        return _pct_cell((c / m - 1) * 100) if c and m else "–"

    get = {
        "quote": lambda r: r.get("quote") or {}, "tech": lambda r: r.get("tech") or {},
        "fund": lambda r: r.get("fund") or {},
    }
    table = [
        row("", [r.get("symbol") for r in results]),
        row("Tín hiệu", [r.get("signal") for r in results]),
        row("Điểm", [score_text(r) for r in results]),
        row("T1 /30", [tier_score_text(r, "tier1") for r in results]),
        row("T2 /30", [tier_score_text(r, "tier2") for r in results]),
        row("T3 /40", [tier_score_text(r, "tier3") for r in results]),
        row("Giá", [fmt_price(get["quote"](r).get("price")) for r in results]),
        row("Hôm nay", [_pct_cell(get["quote"](r).get("percent_change")) for r in results]),
        row("ΔROC63", [_pct_cell(get["tech"](r).get("delta_roc63")) for r in results]),
        row("Giá/MA50", [vs_ma50(r) for r in results]),
        row("RSI14", [fmt_num(get["tech"](r).get("rsi14"), 1) for r in results]),
        row("EPS YoY", [_pct_cell(get["fund"](r).get("eps_yoy")) for r in results]),
        row("ROE", [_pct_cell(get["fund"](r).get("roe")) for r in results]),
        row("Breakout", ["CÓ" if (get["tech"](r).get("breakout") or {}).get("found") else "–" for r in results]),
        row("Bộ lọc", ["PASS" if (r.get("hard") or {}).get("pass") else "FAIL" for r in results]),
    ]
    verdict = (f"🏆 Phù hợp chiến lược nhất: <b>{_esc(best.get('symbol'))}</b>"
               if any(r.get("signal") == "BUY" for r in results) else
               f"⏸ Chưa mã nào đạt BUY · gần tiêu chí nhất: <b>{_esc(best.get('symbol'))}</b>")
    threshold = f" · BUY ≥ {gate['buy_threshold']:g}" if gate.get("buy_threshold") else ""
    lines = [
        f"⚖️ <b>SO SÁNH {' · '.join(_esc(r.get('symbol')) for r in results)}</b>",
        _market_line(gate) + threshold,
        DIVIDER,
        verdict,
        *[f"{SIGNAL_ICONS.get(r.get('signal'), '⚪')} <b>{_esc(r.get('symbol'))}</b> "
          f"{progress_bar(r.get('score_min'), 100)} {score_text(r)} · "
          f"{fmt_price(get['quote'](r).get('price'))} {_arrow(get['quote'](r).get('percent_change'))}"
          for r in results],
        "",
        "<pre>" + html.escape("\n".join(table)) + "</pre>",
    ]
    if text:
        lines += _expandable(["💬 <b>Nhận định</b>", _ai_block(text, 1500)])
    warnings = [f"{r.get('symbol')}: {w}" for r in results for w in (r.get("warnings") or [])[:1]]
    lines += [*_warning_lines(warnings, 3), DISCLAIMER]
    return "\n".join(lines)


# =====================================================================
# Thẻ giá
# =====================================================================
def _quote_block(symbol: str, q: dict) -> list[str]:
    stale = " ⚠️ <i>cache cũ</i>" if q.get("stale_cache") else ""
    when = str(q.get("retrieved_at") or "")
    when = when[11:16] if len(when) >= 16 else when        # ISO -> 'HH:MM'
    price, volume = to_float(q.get("price")), to_float(q.get("volume"))
    value = f" · 💵 GTGD {fmt_money(price * volume)}" if price and volume is not None else ""
    return [
        f"💹 <b>{_esc(symbol)}</b>  <b>{fmt_price(price)}</b> "
        f"{change_text(price, q.get('reference'), q.get('percent_change'))}{stale}",
        f"   📏 {fmt_price(q.get('low'))} {range_bar(q.get('low'), q.get('high'), price)} {fmt_price(q.get('high'))}",
        f"   🔓 Mở {fmt_price(q.get('open'))} · 🔁 Tham chiếu {fmt_price(q.get('reference'))}",
        f"   📦 KL {fmt_volume(volume)}{value}",
        f"   🕒 <i>{_esc(when)} · nguồn {_esc(q.get('data_source'))}</i>",
    ]


def quote_card(symbol: str, q: dict) -> str:
    return "\n".join(["💹 <b>GIÁ HIỆN TẠI</b>", DIVIDER, *_quote_block(symbol, q)])


def multi_quote_card(quotes: dict[str, dict], text: str) -> str:
    lines = ["💹 <b>GIÁ THỊ TRƯỜNG</b>", DIVIDER]
    for i, (symbol, q) in enumerate(quotes.items()):
        lines += ([""] if i else []) + _quote_block(symbol, q)
    # Câu trả lời của planner chỉ lặp lại số liệu trên; chỉ in khi là nhận định khác (từ LLM).
    if text and text.strip() != quote_summary(quotes):
        lines += ["", f"💬 {_ai_block(text, 1500)}"]
    return "\n".join(lines)


# =====================================================================
# Thẻ thị trường
# =====================================================================
def _market_lines(gate: dict, bar_date, stale: bool) -> list[str]:
    close, ma50, dist = to_float(gate.get("close")), to_float(gate.get("ma50")), gate.get("distribution_days")
    if close and ma50:
        gap = (close / ma50 - 1) * 100
        position = f" · {'🔼 trên' if gap > 0 else '🔽 dưới'} MA{STRATEGY.market_ma} {abs(gap):.1f}%"
    else:
        position = ""
    threshold = gate.get("buy_threshold")
    dots = "" if dist is None else " " + "●" * dist + "○" * max(0, STRATEGY.dist_threshold - dist)
    dates = ", ".join(pd.Timestamp(d).strftime("%d/%m") for d in gate.get("distribution_dates") or []) or "—"
    return [
        "🏛 <b>VN-INDEX · CỔNG THỊ TRƯỜNG</b>",
        f"{market_badge(gate)} — {MARKET_HINTS.get(gate.get('state'), 'chưa xác định')}",
        DIVIDER,
        f"📈 VN-Index <b>{fmt_num(close)}</b>{position}",
        f"〰️ MA{STRATEGY.market_ma}: {fmt_num(ma50)}",
        f"📦 Phiên phân phối {'N/A' if dist is None else dist}/{STRATEGY.dist_window}{dots} "
        f"(PRESSURE khi ≥ {STRATEGY.dist_threshold})",
        f"📅 Ngày phân phối: {html.escape(dates)}",
        f"🎯 Ngưỡng BUY: {'⛔ không mở BUY mới' if threshold is None else f'≥ {threshold:g} điểm'}",
        f"⚖️ Tỷ trọng: {_weight_text(gate)}",
        f"💡 <i>{_esc(gate.get('reason'))}</i>",
        *_expandable([
            "📖 <b>Ý nghĩa các trạng thái</b>",
            f"🟢 UPTREND: trên MA50, &lt; {STRATEGY.dist_threshold} phiên phân phối → BUY khi ≥ "
            f"{STRATEGY.buy_score_uptrend:g} điểm",
            f"🟠 PRESSURE: trên MA50, ≥ {STRATEGY.dist_threshold} phiên phân phối → BUY khi ≥ "
            f"{STRATEGY.buy_score_pressure:g}, tỷ trọng giảm nửa",
            "🟡 WEAK: đóng cửa dưới MA50 một phiên → không mua mới",
            "🔴 DOWNTREND: dưới MA50 hai phiên liên tiếp → không mua mới",
            "Phiên phân phối: VN-Index giảm ≥ 0.2% với khối lượng lớn hơn phiên trước.",
        ]),
        f"🕒 <i>Phiên {_esc(bar_date)}</i>" + (" ⚠️ <i>cache cũ</i>" if stale else ""),
    ]


def market_card() -> str:
    """Lệnh /market."""
    try:
        idx, gate, stale = service.get_market_gate()
    except Exception as exc:
        return f"❌ Không lấy được VN-Index: {html.escape(str(exc))}"
    return "\n".join([*_market_lines(gate, service.bar_date(idx), stale), DISCLAIMER])


def market_snapshot_card(snapshot: dict, text: str) -> str:
    """Chat hỏi về thị trường."""
    lines = _market_lines(snapshot.get("gate") or {}, snapshot.get("bar_date"), snapshot.get("stale_cache"))
    if text:
        lines += ["", f"💬 {_ai_block(text, 1200)}"]
    return "\n".join([*lines, DISCLAIMER])


# =====================================================================
# Thẻ danh sách
# =====================================================================
def top_card(rows: list[dict]) -> str:
    """Lệnh /top: `rows` đã được sắp xếp theo thứ hạng."""
    gate = rows[0].get("market") or {}
    buys = sum(r.get("signal") == "BUY" for r in rows)
    threshold = f" · BUY ≥ {gate['buy_threshold']:g}" if gate.get("buy_threshold") else ""
    lines = [f"🏆 <b>BẢNG XẾP HẠNG · {len(rows)} mã</b>", _market_line(gate) + threshold, DIVIDER]
    for i, r in enumerate(rows):
        rank = MEDALS[i] if i < len(MEDALS) else f"{i + 1}."
        q = r.get("quote") or {}
        subtype = f" <i>({_esc(r['hold_type'])})</i>" if r.get("hold_type") else ""
        lines += [
            f"{rank} {SIGNAL_ICONS.get(r.get('signal'), '⚪')} <b>{_esc(r.get('symbol'))}</b> "
            f"{_esc(r.get('signal'))}{subtype}",
            f"   {progress_bar(r.get('score_min'), 100, 8)} <b>{score_text(r)}</b>/100 · "
            f"T1 {tier_score_text(r, 'tier1')}·T2 {tier_score_text(r, 'tier2')}·T3 {tier_score_text(r, 'tier3')}",
            f"   💰 {fmt_price(q.get('price'))} {_arrow(q.get('percent_change'))} "
            f"{fmt_pct(q.get('percent_change'), signed=True)} · ΔROC63 "
            f"{fmt_pct((r.get('tech') or {}).get('delta_roc63'), signed=True)}",
        ]
    lines += [DIVIDER, f"📌 BUY hôm nay: <b>{buys}</b> mã" if buys else "📌 Hôm nay chưa có mã BUY.",
              "<i>Danh sách lấy từ TOP_SYMBOLS trong .env.</i>"]
    return "\n".join(lines)


def watchlist_card(items: dict[str, str | None], minutes: int) -> str:
    lines = ["🔔 <b>WATCHLIST</b>", DIVIDER]
    lines += [f"{SIGNAL_ICONS.get(signal, '⏳')} <b>{_esc(s)}</b> — "
              f"{_esc(signal) if signal else 'chờ lần kiểm tra đầu'}" for s, signal in sorted(items.items())]
    lines.append(f"🕒 <i>Kiểm tra mỗi ~{minutes} phút, chỉ báo khi tín hiệu đổi.</i>")
    return "\n".join(lines)


def positions_card(items: dict[str, dict], prices: dict[str, float | None]) -> str:
    """Vị thế mô phỏng kèm lãi/lỗ tạm tính và các mốc thoát lệnh của chiến lược."""
    lines = ["💼 <b>VỊ THẾ MÔ PHỎNG</b>", DIVIDER]
    for symbol, p in items.items():
        entry, peak, price = p["entry_price"], p["peak_close"], to_float(prices.get(symbol))
        if price is None:
            pnl_line = "   💰 Chưa lấy được giá hiện tại"
        else:
            change = (price / entry - 1) * 100
            pnl_line = f"   💰 Giá {fmt_price(price)} → lãi/lỗ {'🟢' if change >= 0 else '🔴'} <b>{change:+.2f}%</b>"
        trailing = (f"   🪜 Trailing stop {fmt_price(peak * (1 - STRATEGY.trail))}"
                    if peak >= entry * (1 + STRATEGY.trail_trigger) else
                    f"   🪜 Trailing kích hoạt khi giá ≥ {fmt_price(entry * (1 + STRATEGY.trail_trigger))}")
        lines += [
            f"📌 <b>{_esc(symbol)}</b> · mua {fmt_price(entry)} ngày {_esc(p.get('entry_date'))}",
            pnl_line,
            f"   🛑 Cắt lỗ {fmt_price(entry * (1 - STRATEGY.stop))} (−{STRATEGY.stop:.0%}) · "
            f"🏔 đỉnh đã đạt {fmt_price(peak)}",
            trailing,
        ]
    lines.append("<i>Dùng /signal &lt;mã&gt; để engine xét điều kiện bán.</i>")
    return "\n".join(lines)


def agent_text_card(text: str) -> str:
    body = _ai_block(text, 3800)
    return f"💬 {body}" if body else FALLBACK_TEXT


# =====================================================================
# Backtest
# =====================================================================
def _pf_text(value) -> str:
    x = to_float(value)
    if value == float("inf"):
        return "∞ (chưa có lệnh lỗ)"
    return "N/A" if x is None else f"{x:.2f}"


def backtest_menu_text() -> str:
    from config import BACKTEST
    return "\n".join([
        "🧪 <b>BACKTEST</b> &amp; 🔬 <b>KIỂM ĐỊNH TÍN HIỆU</b>",
        DIVIDER,
        "Cả hai đều chạy lại <b>đúng pipeline của bot</b> trên từng phiên quá khứ (dữ liệu tới ngày đó, "
        "BCTC theo ngày công bố), nhưng trả lời hai câu hỏi khác nhau:",
        "",
        "🧪 <b>Backtest danh mục</b> — nếu cầm tiền làm theo bot thì lãi/lỗ bao nhiêu?",
        f"   Vốn {fmt_money(BACKTEST.initial_capital)}, tối đa {BACKTEST.max_positions} vị thế, phí "
        f"{BACKTEST.transaction_fee:.2%}/chiều + thuế bán {BACKTEST.sell_tax:.1%}, khớp Open t+1.",
        "",
        "🔬 <b>Kiểm định độ chính xác</b> — cách tính của bot đúng bao nhiêu case trong quá khứ?",
        "   Chạy lại bot ở từng phiên quá khứ, lấy giá 20 phiên sau làm nhãn thật: BUY đúng khi giá tăng, "
        "SELL khi giảm tiếp, NẮM GIỮ khi không giảm, TRÁNH MUA khi không tăng, CHỜ đo tỷ lệ bỏ lỡ. "
        "So với đoán bừa để biết có đáng tin không.",
        "",
        f"Mặc định {len(BACKTEST.symbols)} mã: {_esc(', '.join(BACKTEST.symbols))}",
        "Gõ thêm: <code>/backtest FPT 3M</code> · <code>/kiemdinh FPT</code> · <code>/kiemdinh FPT 1Y 10</code>",
        "<i>⏳ Lần đầu cần tải dữ liệu (1–5 phút tùy quota VNStock); các lần sau dùng cache.</i>",
    ])


def backtest_progress_text(scope: str, stage: str, done: int, total: int, label: str,
                           title: str = "Đang backtest") -> str:
    step = "📥 Tải dữ liệu lịch sử" if stage == "data" else "⚙️ Dựng lại tín hiệu từng phiên"
    icon = "🔬" if "kiểm định" in title.lower() else "🧪"
    return "\n".join([
        f"{icon} <b>{_esc(title)}</b> {_esc(scope)}",
        f"{step}: {done}/{total}",
        f"{progress_bar(done, total or 1, 12)} {_esc(label)}",
        "<i>Có thể mất vài phút do giới hạn quota VNStock…</i>",
    ])


_BLOCKER_TEXT = {
    "market": "Thị trường không cho mua mới",
    "hard_filter": "Trượt bộ lọc cứng",
    "fundamental_source": "Thiếu BCTC",
    "mandatory_breakout": "Chưa có breakout hợp lệ",
    "score": "Điểm dưới ngưỡng BUY",
}
_BLOCKER_SHORT = {"market": "Thị trường", "hard_filter": "Bộ lọc", "fundamental_source": "BCTC",
                  "mandatory_breakout": "Breakout", "score": "Điểm"}


def _diag_symbol_lines(symbol: str, info: dict) -> list[str]:
    """Chẩn đoán chi tiết một mã: điều kiện nào chặn BUY, bao nhiêu phiên, phiên gần BUY nhất."""
    free = info["free"] or 0
    lines = [f"📌 <b>{_esc(symbol)}</b> — BUY <b>{info['buy_days']}</b> phiên · xét mua {free} phiên"
             + (f" · đang giữ {info['held']} phiên" if info["held"] else "")]
    if not free:
        return lines + ["   Không có phiên nào để xét mua."]
    fail = info["fail"]
    if info["hard_reasons"]:
        for text, n in info["hard_reasons"][:2]:
            lines.append(f"   ❌ Bộ lọc cứng: {_esc(text)} — <b>{n}/{free}</b> phiên")
    elif fail["hard_filter"]:
        lines.append(f"   ❌ Bộ lọc cứng trượt {fail['hard_filter']}/{free} phiên")
    if fail["fundamental_source"]:
        lines.append(f"   ❌ Không có BCTC khả dụng: {fail['fundamental_source']}/{free} phiên")
    lines.append(f"   {'❌' if fail['mandatory_breakout'] else '✅'} Không có breakout hợp lệ: "
                 f"{fail['mandatory_breakout']}/{free} phiên")
    mx = info.get("max_score") or {}
    if mx:
        thr = f" (ngưỡng {mx['threshold']:g})" if mx.get("threshold") is not None else " (thị trường không cho mua)"
        lines.append(f"   {'❌' if fail['score'] else '✅'} Điểm dưới ngưỡng: {fail['score']}/{free} phiên · "
                     f"cao nhất <b>{mx['score']}</b>/100 ngày {mx['date']:%d/%m}{thr}")
    if info["avoid_days"]:
        lines.append(f"   ⛔ Tránh mua (dưới MA50 dốc xuống / breakdown): {info['avoid_days']} phiên")
    best = info.get("best")
    if best and info["buy_days"] == 0:
        miss = ", ".join(best["missing"]) or "không"
        lines.append(f"   🔎 Gần BUY nhất {best['date']:%d/%m}: đạt {best['passed']}/{best['total']} điều kiện, "
                     f"thiếu <b>{_esc(miss)}</b> (điểm {best['score']})")
    last = info.get("last") or {}
    if last.get("score") is not None:
        lines.append(f"   🧾 Phiên cuối {last['date']:%d/%m}: {_esc(last['signal'])} · {last['score']}/100 "
                     f"<i>(phải khớp với thẻ /signal cùng ngày)</i>")
    if info["errors"]:
        lines.append(f"   ⚠️ {info['errors']} phiên không tính được chỉ báo")
    return lines


def _diag_table(per_symbol: dict) -> list[str]:
    """Bảng gọn cho danh mục nhiều mã (đặt ngoài blockquote theo quy tắc Telegram)."""
    rows = ["Mã    BUY  Max  Nút thắt chính"]   # BUY = số phiên có tín hiệu BUY, Max = điểm cao nhất
    for s, info in per_symbol.items():
        mx = (info.get("max_score") or {}).get("score")
        blocker = info.get("main_blocker")
        text = "—" if not blocker else f"{_BLOCKER_SHORT[blocker[0]]} {blocker[1]}/{blocker[2]}"
        rows.append(f"{s:<5} {info['buy_days']:>4} {'-' if mx is None else mx:>4}  {text}")
    return ["<pre>" + html.escape("\n".join(rows)) + "</pre>",
            "<i>BUY = số phiên có tín hiệu BUY · Max = điểm cao nhất trong kỳ · Nút thắt = điều kiện trượt "
            "nhiều nhất trong các phiên thị trường cho mua (số phiên trượt/số phiên đó).</i>"]


def backtest_card(report: dict) -> str:
    """Kết quả backtest: kết luận -> hiệu quả -> VÌ SAO có/không có lệnh -> tham số, cách đọc (thu gọn)."""
    m = report["metrics"]
    cfg = report["config"]
    diag = report.get("diagnostics") or {}
    per_symbol = diag.get("per_symbol") or {}
    symbols = report["symbols"]
    ret, vret = m["total_return"], m["vnindex_return"]
    no_trade = m["n_orders"] == 0
    scope = _esc(", ".join(symbols)) if len(symbols) <= 4 else f"{len(symbols)} mã"
    lines = [
        f"🧪 <b>KẾT QUẢ BACKTEST</b> · {scope}",
        f"📅 {report['start']:%d/%m/%Y} → {report['end']:%d/%m/%Y} · {report['sessions']} phiên",
        DIVIDER,
    ]
    if no_trade:
        lines += [
            "⚪ <b>Chiến lược KHÔNG mở lệnh nào trong giai đoạn này.</b>",
            f"Danh mục giữ 100% tiền mặt ({fmt_money(m['initial_capital'])}) nên lợi nhuận = 0%; "
            "Win rate, Profit Factor, Drawdown không có giao dịch để tính.",
            f"🏛 Tham chiếu: VN-Index cùng kỳ {fmt_pct(vret, signed=True, digits=2)} "
            f"(sụt sâu nhất {fmt_pct(m['vnindex_mdd'], digits=1)}).",
        ]
    else:
        icon = "🟢" if ret > 0.005 else "🔴" if ret < -0.005 else "⚪"
        excess = m["excess_return"]
        vs = "" if excess is None else (f" → danh mục {'hơn' if excess >= 0 else 'kém'} "
                                        f"<b>{abs(excess):.2f} điểm %</b>")
        lines += [
            f"💼 Vốn {fmt_money(m['initial_capital'])} → <b>{fmt_money(m['final_equity'])}</b>",
            f"{icon} Lợi nhuận danh mục (Total Return): <b>{ret:+.2f}%</b>",
            f"🏛 VN-Index cùng kỳ {fmt_pct(vret, signed=True, digits=2)}{vs}",
            "",
            "📊 <b>HIỆU QUẢ</b>",
            f"🔁 Giao dịch đã đóng: <b>{m['n_trades']}</b> (thắng {m['n_wins']} · thua {m['n_losses']}) · "
            f"Win rate <b>{fmt_pct(m['win_rate'])}</b>",
            f"➕ Lãi TB/lệnh thắng {fmt_pct(m['avg_win_pct'], signed=True, digits=2)} · "
            f"➖ Lỗ TB/lệnh thua {fmt_pct(m['avg_loss_pct'], signed=True, digits=2)}",
            f"⚖️ Profit Factor: <b>{_pf_text(m['profit_factor'])}</b> <i>(tổng lãi ÷ tổng lỗ; &gt;1 là có lời)</i>",
            f"📉 Sụt giảm tối đa: <b>{fmt_pct(m['max_drawdown'], digits=2)}</b> · "
            f"VN-Index {fmt_pct(m['vnindex_mdd'], digits=2)}",
            f"⏱ Nắm giữ TB {fmt_num(m['avg_holding_sessions'], 1)} phiên · 💸 phí + thuế "
            f"{fmt_money(m['total_fees'] + m['total_tax'])} đ · vốn đầu tư TB {fmt_pct(m['avg_exposure'], digits=0)}",
        ]
        trips = report.get("trips") or []
        if trips:
            ordered = sorted(trips, key=lambda t: -t["pnl_pct"])
            show = ordered[:3] + [t for t in ordered[-2:] if t not in ordered[:3] and t["pnl_pct"] < 0]
            lines.append("🗂 <b>Giao dịch nổi bật</b>")
            lines += [f"   {'🟢' if t['pnl'] > 0 else '🔴'} <b>{_esc(t['symbol'])}</b> {t['pnl_pct']:+.1f}% · "
                      f"mua {t['entry_date']:%d/%m} → bán {t['exit_date']:%d/%m} ({t['holding_sessions']} phiên)"
                      for t in show]

    # ---- Vì sao có / không có lệnh
    sc = report.get("signal_counts") or {}
    lines += ["", f"🔍 <b>{'VÌ SAO KHÔNG CÓ LỆNH?' if no_trade else 'CHẨN ĐOÁN TÍN HIỆU'}</b>"]
    if diag.get("market_total"):
        gates = report.get("gate_days") or {}
        blocked = ", ".join(f"{k} {v}" for k, v in gates.items() if k not in ("UPTREND", "PRESSURE"))
        lines.append(f"🏛 Cổng thị trường chặn mua mới <b>{diag['market_blocked']}/{diag['market_total']}</b> "
                     f"phiên" + (f" ({_esc(blocked)})" if blocked else ""))
    lines.append(f"📡 Số lần có tín hiệu: BUY {sc.get('BUY', 0)} · SELL {sc.get('SELL', 0)} · lệnh khớp {m['n_orders']}")
    if m["n_skipped"]:
        parts = " · ".join(f"{_esc(k)} {v}" for k, v in sorted(m["skipped_by_reason"].items(), key=lambda kv: -kv[1]))
        lines.append(f"🚫 BUY không khớp được: {m['n_skipped']} ({parts})")
    if len(per_symbol) <= 3:
        for sym, info in per_symbol.items():
            lines += _diag_symbol_lines(sym, info)
    else:
        lines += _diag_table(per_symbol)
        lines += _expandable(["🧾 <b>Đối chiếu phiên cuối với /signal</b>"] + [
            f"{_esc(sym)}: {_esc((info.get('last') or {}).get('signal'))} · "
            f"{(info.get('last') or {}).get('score')}/100" for sym, info in per_symbol.items()])
    if no_trade:
        lines.append("<i>👉 Bot chỉ BUY khi đủ 6/6 điều kiện (thị trường, bộ lọc, BCTC, breakout, điểm, chưa giữ). "
                     "Nhật ký từng phiên nằm trong file CSV ở nút 📄.</i>")

    gates = report.get("gate_days") or {}
    detail = [
        "⚙️ <b>Tham số mô phỏng</b>",
        f"• Vốn {fmt_money(m['initial_capital'])}, tối đa {cfg['max_positions']} vị thế chia đều"
        + (" · PRESSURE giảm ½ tỷ trọng" if cfg["apply_weight_factor"] else ""),
        f"• Phí {cfg['transaction_fee']:.2%}/chiều · thuế bán {cfg['sell_tax']:.1%} · lô {cfg['lot_size']} cp",
        f"• Tín hiệu cuối ngày t → khớp giá mở cửa t+1 · bán sau T+{cfg['settlement_days']} · "
        "bỏ qua lệnh mua ở giá trần",
        f"• BCTC quý chỉ dùng sau {cfg['fund_lag_days']} ngày kể từ cuối quý · không margin",
        "• Vị thế còn lại được đóng theo giá đóng cửa phiên cuối" if cfg["close_at_end"] else
        "• Vị thế còn lại giữ nguyên (định giá theo giá đóng cửa)",
    ]
    if gates:
        detail.append("🏛 Số phiên theo cổng thị trường: " + " · ".join(
            f"{MARKET_ICONS.get(k, '⚪')} {_esc(k)} {v}" for k, v in sorted(gates.items(), key=lambda kv: -kv[1])))
    optional = _expandable(detail) + _expandable([
        "📖 <b>Cách đọc</b>",
        "• Total Return: lãi/lỗ của cả danh mục (tính cả tiền mặt chưa dùng).",
        "• Win rate: % giao dịch có lãi. Profit Factor: tổng lãi ÷ tổng lỗ.",
        "• Sụt giảm tối đa (MDD): mức giảm sâu nhất từ đỉnh tài sản.",
        "• Số lần có tín hiệu BUY đếm theo (phiên × mã); một mã BUY nhiều phiên liền nhưng chỉ mua một lần.",
        "• Backtest một mã chỉ giao dịch khi riêng mã đó đạt BUY; kế hoạch Backtest dùng danh mục 15 mã.",
    ])
    if report.get("warnings"):
        optional = _expandable(["⚠️ <b>Kiểm tra dữ liệu</b>",
                                *[f"• {_esc(_clip(w, 140))}" for w in report["warnings"][:8]]]) + optional
    footer = f"<i>⏱ {report.get('runtime_seconds', 0)} giây · ℹ️ Kết quả quá khứ không đảm bảo lợi nhuận tương lai.</i>"
    # Quá giới hạn 4096 ký tự của Telegram: bỏ dần khối phụ (mỗi khối là một phần tử trọn vẹn, không cắt ngang thẻ HTML).
    while optional and len("\n".join(lines + optional + [footer])) > 4000:
        optional.pop()
    return "\n".join(lines + optional + [footer])


# =====================================================================
# Kiểm định tín hiệu quá khứ
# =====================================================================
def _pc(x, digits: int = 0) -> str:
    return "–" if x is None else f"{x:.{digits}f}%"


def validation_card(report: dict, crosscheck: list[dict] | None = None) -> str:
    """Kiểm định độ chính xác: bot đúng bao nhiêu case trong quá khứ, theo từng kết luận, so với đoán bừa,
    kết luận hôm nay đáng tin tới đâu, và nhận xét bằng lời."""
    from signal_validation import MISSED_GAIN, RULES, SCORED_LABELS, WAIT_LABELS, _reliability_text

    acc = report["accuracy"]
    h, o = acc["h"], acc["overall"]
    symbols = report["symbols"]
    scope = _esc(", ".join(symbols)) if len(symbols) <= 4 else f"{len(symbols)} mã"
    lines = [
        f"🔬 <b>KIỂM ĐỊNH ĐỘ CHÍNH XÁC</b> · {scope}",
        f"📅 Tín hiệu {report['start']:%d/%m/%Y} → {report['end']:%d/%m/%Y} · chấm bằng giá <b>{h} phiên sau</b>",
        DIVIDER,
    ]
    if o["n"]:
        lines += [f"🎯 <b>BOT ĐÚNG {o['correct']}/{o['n']} CASE = {o['acc']:.0f}%</b>",
                  f"🎲 Đoán bừa (theo tỷ lệ tăng/giảm tự nhiên của mã): {o['guess']:.0f}% → bot "
                  f"<b>{'hơn' if o['diff'] >= 0 else 'kém'} {abs(o['diff']):.0f} điểm %</b>",
                  f"{o['icon']} {_esc(_reliability_text(o))}"]
    else:
        lines.append("🎯 Chưa có case BUY/SELL/NẮM GIỮ/TRÁNH MUA nào đủ tương lai để chấm.")
    if acc["n_unscored"]:
        lines.append(f"<i>{acc['n_unscored']} phiên từ {acc['unscored_from']:%d/%m} chưa đủ {h} phiên tương lai "
                     "nên chưa chấm.</i>")

    # ---- theo từng kết luận
    rows = ["Kết luận   Đúng/Chấm  Đúng  Bừa"]
    for lab in (*SCORED_LABELS, *WAIT_LABELS):
        v = acc["by_label"].get(lab)
        if not v:
            continue
        if lab in SCORED_LABELS:
            frac = f"{v['correct']}/{v['n']}"
            rows.append(f"{lab:<10} {frac:>9} {_pc(v['acc']):>5} {_pc(v['guess']):>4}")
        else:
            frac = f"{v['missed']}/{v['scored']}"
            rows.append(f"{lab:<10} {frac:>9} bỏ lỡ {_pc(v['missed_rate'])}")
    if o["n"]:
        rows.append(f"{'TỔNG':<10} {str(o['correct']) + '/' + str(o['n']):>9} {_pc(o['acc']):>5} {_pc(o['guess']):>4}")
    rules = " · ".join(f"{k} = {v}" for k, v in RULES.items() if k != "CHỜ")
    lines += ["", "📋 <b>THEO TỪNG KẾT LUẬN</b>", "<pre>" + html.escape("\n".join(rows)) + "</pre>",
              f"<i>Đúng khi: {_esc(rules)}. CHỜ: bỏ lỡ = sau đó tăng &gt; {MISSED_GAIN:g}%.</i>"]
    if "BUY" not in acc["by_label"]:
        lines.append("⚠️ Không có BUY nào trong giai đoạn này → <b>chưa kiểm chứng được độ chính xác của BUY</b>.")

    # ---- thực tế sau h phiên (ma trận nhầm lẫn)
    if acc["confusion"]:
        crow = ["Bot nói      TĂNG NGANG GIẢM"]
        for lab, cnt in acc["confusion"].items():
            crow.append(f"{lab:<11} {cnt['TĂNG']:>5} {cnt['ĐI NGANG']:>5} {cnt['GIẢM']:>4}")
        lines += ["", f"🧮 <b>THỰC TẾ {h} PHIÊN SAU</b> (số case)", "<pre>" + html.escape("\n".join(crow)) + "</pre>"]

    # ---- theo tầm nhìn
    hz = report.get("horizons") or []
    if hz:
        lines.append("⏱ Theo tầm nhìn: " + " · ".join(
            f"{r['h']} phiên <b>{_pc(r['acc'])}</b> (bừa {_pc(r['guess'])})" for r in hz))

    # ---- hôm nay
    lines += ["", "🧭 <b>KẾT LUẬN HÔM NAY ĐÁNG TIN TỚI ĐÂU?</b>"]
    shown = 6 if len(symbols) <= 6 else 4
    for sym, v in list(acc["per_symbol"].items())[:shown]:
        head = f"• <b>{_esc(sym)}</b> {v['today_date']:%d/%m}: bot nói <b>{_esc(v['today'])}</b> ({v['today_score']}/100)"
        th, ta = v.get("today_hist"), v.get("today_all")
        if th and th["n"]:
            head += (f" → quá khứ khi nói {_esc(v['today'])} với {_esc(sym)}: đúng {th['correct']}/{th['n']} "
                     f"= {th['acc']:.0f}% (bừa {th['guess']:.0f}%)")
        elif ta and ta["n"]:
            head += f" → {_esc(sym)} chưa có case này; mọi mã: đúng {ta['correct']}/{ta['n']} = {ta['acc']:.0f}%"
        elif v.get("today_missed"):
            missed, total = v["today_missed"]
            head += f" → quá khứ khi CHỜ: {missed}/{total} lần bỏ lỡ nhịp tăng &gt; {MISSED_GAIN:g}%"
        else:
            head += " → quá khứ chưa có case tương tự để đối chiếu"
        lines.append(head)
    if len(acc["per_symbol"]) > shown:
        lines.append(f"<i>… và {len(acc['per_symbol']) - shown} mã khác trong file CSV.</i>")

    # ---- theo mã (nhiều mã) / theo tháng (1 mã)
    if len(symbols) > 1:
        srow = ["Mã     Đúng/Chấm  Đúng  Bừa"]
        for sym, v in acc["per_symbol"].items():
            srow.append(f"{sym:<6} {str(v['correct']) + '/' + str(v['n']):>9} {_pc(v['acc']):>5} {_pc(v['guess']):>4}")
        lines += ["", "🏷 <b>THEO MÃ</b>", "<pre>" + html.escape("\n".join(srow)) + "</pre>"]
    elif acc["months"]:
        mrow = ["Tháng    Đúng/Chấm  Đúng"]
        for m in acc["months"]:
            mrow.append(f"{m['month']:<8} {str(m['correct']) + '/' + str(m['n']):>9} {_pc(m['acc']):>5}")
        lines += ["", "📆 <b>THEO THÁNG</b>", "<pre>" + html.escape("\n".join(mrow)) + "</pre>"]

    # ---- nhận xét bằng lời
    comments = report.get("commentary") or []
    lines += ["", "💬 <b>NHẬN XÉT</b>"]
    if len(comments) > 1:
        lines.append(f"• {_esc(comments[1])}")
    if comments:
        lines.append(f"<b>{_esc(comments[-1])}</b>")

    # Khối thu gọn, kèm độ ưu tiên: thiếu chỗ thì bỏ khối ưu tiên thấp trước.
    blocks: list[tuple[int, str]] = []
    if report.get("warnings"):
        blocks += [(5, b) for b in _expandable(["⚠️ <b>Kiểm tra dữ liệu</b>",
                                                *[f"• {_esc(_clip(w, 140))}" for w in report["warnings"][:6]]])]
    detail = comments[2:-1]
    if detail:
        blocks += [(4, b) for b in _expandable(["📝 <b>Nhận xét chi tiết</b>", *[f"• {_esc(c)}" for c in detail]])]
    if crosscheck:
        blocks += [(4, b) for b in _expandable(
            ["🔗 <b>Kiểm tra pipeline</b> (phiên cuối replay vs engine live /signal)",
             *[f"{'⚪' if c['ok'] is None else '✅' if c['ok'] else '❌'} {_esc(c['symbol'])}: {_esc(c['detail'])}"
               for c in crosscheck]])]
    blocks += [(3, b) for b in _expandable([
        "📖 <b>Cách chấm</b>",
        "• Mỗi phiên quá khứ, bot được chạy lại y hệt hôm đó (chỉ dữ liệu tới ngày đó, BCTC đã công bố).",
        f"• Sau đó mới lấy giá {h} phiên SAU làm nhãn thật (TĂNG/GIẢM) để chấm kết luận đúng hay sai.",
        "• Đoán bừa = nói cùng kết luận đó mà không nhìn gì: VD BUY đúng với xác suất = tỷ lệ phiên mã tăng.",
        "• ✅/❌ = khác đoán bừa rõ ràng · 🟡 = có dấu hiệu · ⚪ = chưa khác (có thể may rủi). "
        f"Các cửa sổ {h} phiên chồng nhau đã được hiệu chỉnh.",
    ])]
    tr = acc["trades"]
    if tr["n"]:
        blocks += [(2, b) for b in _expandable([f"🔁 <b>Nếu làm đúng BUY→SELL</b>: {tr['n']} lệnh, thắng {tr['win']}, "
                                                f"TB {tr['avg']:+.2f}%/lệnh sau phí + thuế"])]
    blocks += [(1, b) for b in _expandable(["🧪 <b>Phân tích nâng cao</b> (lợi nhuận TB, IC điểm số, cổng thị trường)",
                                            *[f"{icon} {_esc(text)}" for icon, text in report.get("verdicts") or []]])]
    footer = f"<i>⏱ {report.get('runtime_seconds', 0)} giây · từng case + nhận xét ở nút 📄 (CSV).</i>"
    # Quá giới hạn Telegram: bỏ khối ưu tiên thấp trước (mỗi khối là một phần tử trọn vẹn, không cắt thẻ HTML).
    while blocks and len("\n".join(lines + [b for _, b in blocks] + [footer])) > 4000:
        blocks.remove(min(blocks, key=lambda pb: pb[0]))
    return "\n".join(lines + [b for _, b in blocks] + [footer])
