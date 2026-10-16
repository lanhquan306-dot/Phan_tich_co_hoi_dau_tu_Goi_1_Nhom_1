"""Bộ lập kế hoạch hội thoại và bộ viết câu trả lời theo quy tắc (không gọi mạng, không gọi LLM).

Hai việc chính:
    1. Hiểu câu hỏi: phân loại ý định (quote / analysis / compare / explain /
       market / strategy / general) và suy ra mã đang được nói tới, kể cả khi
       người dùng chỉ viết "nó", "tại sao?", "so với VIC thì sao?".
    2. Viết câu trả lời từ kết quả engine đã tính sẵn. Nhờ vậy bot vẫn trả lời
       đầy đủ khi Groq hết quota hoặc lỗi; Groq chỉ là lớp làm mượt câu chữ.
"""
from __future__ import annotations

from utils import contains_any, fmt_num, fmt_pct, fmt_price, normalize_text, padded, to_float

FALLBACK_TEXT = "Mình chưa thể trả lời câu này ở thời điểm hiện tại. Bạn có thể thử lại sau."

# ---------------------------------------------------------------- từ khóa (đã bỏ dấu)
COMPARE_KEYS = (
    "so sanh", "so voi", "voi nhau", "ma nao", "cai nao", "tot hon",
    "manh hon", "yeu hon", "phu hop hon", "chon", "versus", " vs ",
)
QUOTE_KEYS = (
    "gia bao nhieu", "gia hien tai", "gia hom nay", "gia luc nay", "gia cua",
    "bao nhieu tien", "quote", "price", "open", "high", "low", "ohlc",
    "khoi luong", "volume", "tham chieu",
)
ACTION_KEYS = ("nen mua", "mua khong", "mua ko", "buy", "hold", "sell", "phan tich", "danh gia")
EXPLAIN_KEYS = (
    "tai sao", "vi sao", "sao lai", "giai thich", "nghia la gi", "la gi",
    "chi co", "chi duoc", "thieu", "can gi", "can lam gi", "dieu kien gi",
    "khi nao", "t1", "t2", "t3", "hard filter", "bo loc", "breakout",
)
MARKET_KEYS = (
    "vn index", "vnindex", "thi truong", "market gate", "uptrend", "pressure",
    "downtrend", "phien phan phoi",
)
STRATEGY_KEYS = (
    "chien luoc", "canslim", "rule engine", "quy tac", "nguong buy",
    "cach cham", "cham diem", "t1 la gi", "t2 la gi", "t3 la gi",
)
# Câu có các cụm này mà không nêu mã -> hiểu là đang nói về mã vừa hỏi.
REFERENTIAL_KEYS = (
    "no", "ma nay", "co phieu nay", "cai nay", "con no", "the no", "vay no",
    "tai sao", "vi sao", "mua khong", "breakout", "t1", "t2", "t3",
    "hard filter", "diem", "gia no", "gia nay", "can gi", "khi nao",
)
FOLLOWUP_ACTION_KEYS = (
    "phan tich di", "phan tich tiep", "danh gia di", "nen mua khong",
    "mua duoc khong", "tin hieu sao", "xem ky hon", "noi ky hon",
)
REFRESH_KEYS = ("hom nay", "hien tai", "luc nay", "moi nhat", "cap nhat", "bay gio", "realtime")


# ---------------------------------------------------------------- hiểu câu hỏi
def classify_intent(question: str, explicit_symbols: list[str] | None = None,
                    previous_results: dict[str, dict] | None = None) -> str:
    """Phân loại ý định theo từ khóa, không tốn lượt gọi LLM.

    Thứ tự kiểm tra có chủ đích: so sánh > hỏi giá > thị trường > chiến lược >
    giải thích câu trước > phân tích. "giá FPT bao nhiêu" chỉ là quote, còn
    "giá FPT có nên mua không" là analysis vì ý định hành động mạnh hơn.
    """
    q = padded(question)
    symbols = explicit_symbols or []
    has_previous = bool(previous_results)

    if len(symbols) >= 2 or contains_any(q, COMPARE_KEYS):
        return "compare"
    quote_like = contains_any(q, QUOTE_KEYS) or (" gia " in q and (" bao nhieu " in q or bool(symbols)))
    if quote_like and not contains_any(q, ACTION_KEYS):
        return "quote"
    if not symbols and contains_any(q, MARKET_KEYS):
        return "market"
    if not symbols and contains_any(q, STRATEGY_KEYS):
        return "strategy"
    if has_previous and contains_any(q, EXPLAIN_KEYS):
        return "explain"
    if symbols or (has_previous and contains_any(q, FOLLOWUP_ACTION_KEYS)):
        return "analysis"
    if has_previous and contains_any(q, REFERENTIAL_KEYS):
        return "explain"
    return "general"


def contextual_symbols(question: str, explicit_symbols: list[str],
                       previous_results: dict[str, dict] | None = None, max_symbols: int = 5) -> list[str]:
    """Bổ sung mã từ ngữ cảnh: 'so với VIC' sau khi hỏi OCB -> [OCB, VIC]; 'tại sao?' -> [OCB]."""
    previous = list((previous_results or {}).keys())
    last = previous[-1] if previous else None
    q = padded(question)
    out = list(dict.fromkeys(explicit_symbols))

    if len(out) >= 2 or contains_any(q, COMPARE_KEYS):
        if len(out) == 1 and last and last not in out:
            out.insert(0, last)
        elif not out:
            out = previous[-2:]
    elif not out and last and (contains_any(q, REFERENTIAL_KEYS) or contains_any(q, FOLLOWUP_ACTION_KEYS)):
        out = [last]
    return out[:max(1, int(max_symbols))]


def should_reuse_previous(question: str, intent: str) -> bool:
    """Câu hỏi 'tại sao' dùng lại kết quả cũ, trừ khi người dùng đòi số liệu mới."""
    return intent == "explain" and not contains_any(padded(question), REFRESH_KEYS)


# ---------------------------------------------------------------- điểm số
def score_text(r: dict) -> str:
    """'70' hoặc '62–77' khi T2.1 thiếu dữ liệu."""
    if r.get("score_complete", True):
        value = r.get("score") if r.get("score") is not None else r.get("score_min")
        return str(value if value is not None else "N/A")
    return f"{r.get('score_min', 'N/A')}–{r.get('score_max', 'N/A')}"


def tier_score_text(r: dict, key: str) -> str:
    """Điểm một tầng: key là 'tier1' / 'tier2' / 'tier3'."""
    tier = r.get(key) or {}
    if key == "tier2" and not tier.get("score_complete", True):
        return f"{tier.get('score_min', 'N/A')}–{tier.get('score_max', 'N/A')}"
    value = tier.get("score") if tier.get("score") is not None else tier.get("score_min")
    return str(value if value is not None else "N/A")


def tiers_line(r: dict) -> str:
    return (f"T1 {tier_score_text(r, 'tier1')}/30 · T2 {tier_score_text(r, 'tier2')}/30 · "
            f"T3 {tier_score_text(r, 'tier3')}/40")


def _failed_hard_checks(r: dict) -> list[str]:
    checks = (r.get("hard") or {}).get("checks") or {}
    return [str(v.get("detail")) for v in checks.values() if not v.get("ok") and v.get("detail")]


def _core_blockers(r: dict) -> list[str]:
    """Các lý do chính khiến một mã chưa được BUY."""
    blockers: list[str] = []
    market = r.get("market") or {}
    breakout = (r.get("tech") or {}).get("breakout") or {}

    if not market.get("allow_buy", False):
        blockers.append(f"cổng thị trường {market.get('state', 'UNKNOWN')} chưa cho mở BUY mới")
    if not (r.get("hard") or {}).get("pass", False):
        failed = _failed_hard_checks(r)
        blockers.append("hard filter chưa đạt" + (f" ({'; '.join(failed[:2])})" if failed else ""))
    if (r.get("checks") or {}).get("mandatory_breakout") is False or not breakout.get("found"):
        blockers.append("chưa có breakout hợp lệ trong cửa sổ của chiến lược")

    threshold = to_float(r.get("threshold"))
    best = to_float(r.get("score" if r.get("score_complete", True) else "score_max"))
    if threshold is not None and best is not None and best < threshold:
        blockers.append(f"điểm {best:g}/100 vẫn dưới ngưỡng BUY {threshold:g}")
    return blockers


def _rank_key(r: dict):
    """Sắp xếp mô tả khi so sánh: tín hiệu > điểm > Delta ROC63. Không tạo khuyến nghị mới."""
    rank = {"BUY": 3, "HOLD": 2, "SELL": 1}
    score = to_float(r.get("score") if r.get("score_complete", True) else r.get("score_min"))
    delta = to_float((r.get("tech") or {}).get("delta_roc63"))
    return (rank.get(str(r.get("signal")), 0), -1 if score is None else score, -1e9 if delta is None else delta)


# ---------------------------------------------------------------- viết câu trả lời
def single_analysis_summary(r: dict) -> str:
    """Kết luận + cấu trúc điểm + nút thắt + động lượng + cơ bản + điều cần theo dõi."""
    symbol = str(r.get("symbol") or "Mã")
    signal = str(r.get("signal") or "N/A")
    threshold = to_float(r.get("threshold"))
    state = (r.get("market") or {}).get("state", "N/A")
    tech = r.get("tech") or {}
    fund = r.get("fund") or {}
    breakout = tech.get("breakout") or {}

    if signal == "BUY":
        first = f"{symbol} hiện đạt BUY theo đúng rule engine với {score_text(r)}/100"
        first += f", vượt ngưỡng {threshold:g} của thị trường {state}." if threshold is not None else "."
    elif signal == "SELL":
        first = f"{symbol} hiện bị engine đánh dấu SELL."
    else:
        first = f"{symbol} hiện là HOLD {score_text(r)}/100"
        if threshold is not None:
            base = to_float(r.get("score") if r.get("score_complete", True) else r.get("score_min"))
            first += f"; thị trường {state} yêu cầu BUY ≥ {threshold:g}"
            if base is not None and base < threshold:
                first += f", tức còn thiếu {threshold - base:g} điểm"
            elif base is not None:
                first += ", điểm đã đạt nhưng còn điều kiện bắt buộc khác chưa thỏa"
        first += "."

    lines = [first, f"• Cấu trúc điểm: {tiers_line(r)}."]
    if signal != "BUY":
        blockers = _core_blockers(r)
        if blockers:
            lines.append("• Nút thắt chính: " + "; ".join(blockers[:3]) + ".")

    close, ma50 = to_float(tech.get("close")), to_float(tech.get("ma50"))
    delta, rsi, vol = (to_float(tech.get(k)) for k in ("delta_roc63", "rsi14", "vol_ratio_3d"))
    momentum = []
    if close is not None and ma50 is not None:
        momentum.append("Close trên MA50" if close >= ma50 else "Close dưới MA50")
    if tech.get("ma50_down"):
        momentum.append("MA50 đang dốc xuống")
    if delta is not None:
        momentum.append(f"ΔROC63 {fmt_pct(delta, signed=True)}")
    if rsi is not None:
        momentum.append(f"RSI14 {fmt_num(rsi, 1)}")
    if vol is not None:
        momentum.append(f"Vol {fmt_num(vol, 2)}x")
    if momentum:
        lines.append("• Momentum: " + " · ".join(momentum) + ".")

    basics = []
    if to_float(fund.get("eps_yoy")) is not None:
        basics.append(f"EPS YoY {fmt_pct(fund.get('eps_yoy'))}")
    if to_float(fund.get("roe")) is not None:
        basics.append(f"ROE TTM {fmt_pct(fund.get('roe'))}")
    if basics:
        lines.append("• Cơ bản: " + " · ".join(basics) + ".")

    if signal == "BUY" and breakout.get("found"):
        lines.append(f"• Kỷ luật vào lệnh của engine: pivot {fmt_price(r.get('breakout_pivot'))}; "
                     f"buy zone tối đa {fmt_price(r.get('buy_zone'))}.")
    elif signal != "BUY":
        watch = []
        if close is not None and ma50 is not None and close < ma50:
            watch.append("giá lấy lại MA50 và xu hướng MA50 cải thiện")
        if not breakout.get("found"):
            watch.append("xuất hiện breakout hợp lệ")
        if vol is not None and vol < 1.2:
            watch.append("khối lượng xác nhận tốt hơn")
        if watch:
            lines.append("• Điều cần theo dõi tiếp: " + "; ".join(watch) + ".")
    return "\n".join(lines)


def explanation_summary(r: dict, question: str) -> str:
    """Trả lời câu hỏi nối tiếp: 'tại sao?', 'T3 vì sao bằng 0?', 'hard filter là gì?'."""
    q = padded(question)
    symbol = str(r.get("symbol") or "Mã")

    if "hard filter" in q or "bo loc" in q:
        if (r.get("hard") or {}).get("pass"):
            return (f"{symbol} đang PASS hard filter: sàn, thanh khoản, lịch sử và điều kiện nợ đều đạt. "
                    f"Hard filter không phải nguyên nhân khiến {symbol} chưa BUY.")
        detail = "; ".join(_failed_hard_checks(r)) or "có ít nhất một điều kiện chưa đạt"
        return (f"{symbol} FAIL hard filter vì {detail}. Đây là điều kiện bắt buộc: dù tổng điểm cao, "
                "mã vẫn không được phát BUY cho tới khi bộ lọc này đạt.")

    requested = "T3" if "breakout" in q else next((k for k in ("T1", "T2", "T3") if f" {k.lower()} " in q), None)
    if requested:
        key = "tier" + requested[1]
        max_score = {"T1": 30, "T2": 30, "T3": 40}[requested]
        lines = [f"{symbol}: {requested} hiện {tier_score_text(r, key)}/{max_score}."]
        for code, comp in ((r.get(key) or {}).get("components") or {}).items():
            points = "N/A" if comp.get("points") is None else comp.get("points")
            lines.append(f"• {code}: {points}/{comp.get('max', 'N/A')} — {str(comp.get('detail') or '').strip()}")
        if requested == "T3" and not ((r.get("tech") or {}).get("breakout") or {}).get("found"):
            lines.append("• Breakout là điều kiện bắt buộc của BUY nên T3.1 = 0 là một blocker trực tiếp.")
        return "\n".join(lines)

    lines = [f"{symbol} đang {r.get('signal')} {score_text(r)}/100. Các điều kiện BUY phải đồng thời thỏa, "
             "nên lý do không nằm ở một con số duy nhất."]
    items = _core_blockers(r) or [str(x).rstrip(".") for x in (r.get("reasons") or [])]
    lines += [f"• {item}." for item in items[:4]]
    lines.append(f"• Điểm hiện tại: {tiers_line(r)}.")
    return "\n".join(lines)


def comparison_summary(results: dict[str, dict]) -> str:
    """So sánh nhiều mã theo đúng kết quả engine; không ép chọn mã để mua khi không mã nào BUY."""
    rows = list(results.values())
    if not rows:
        return "Chưa có đủ dữ liệu để so sánh."
    top = max(rows, key=_rank_key)
    any_buy = any(r.get("signal") == "BUY" for r in rows)
    if any_buy:
        intro = (f"Theo đúng strategy của bot, {top.get('symbol')} đang có trạng thái thuận lợi hơn trong nhóm "
                 f"vì engine đã xếp {top.get('signal')} {score_text(top)}/100.")
    else:
        intro = (f"Hiện chưa mã nào trong nhóm đạt BUY. {top.get('symbol')} đang gần bộ tiêu chí của strategy hơn, "
                 f"nhưng vẫn chỉ là {top.get('signal')} {score_text(top)}/100.")

    lines = [intro]
    for r in rows:
        tech = r.get("tech") or {}
        lines.append(
            f"• {r.get('symbol')}: {r.get('signal')} {score_text(r)}/100 · {tiers_line(r)} · "
            f"ΔROC63 {fmt_pct(tech.get('delta_roc63'), signed=True)} · "
            f"breakout {'CÓ' if (tech.get('breakout') or {}).get('found') else 'CHƯA'} · "
            f"hard {'PASS' if (r.get('hard') or {}).get('pass') else 'FAIL'}."
        )
    if not any_buy:
        lines.append("• Kết luận: chưa nên biến phép so sánh này thành tín hiệu mua; cần chờ ít nhất một mã "
                     "thực sự vượt các điều kiện BUY của engine.")
    return "\n".join(lines)


def quote_summary(quotes: dict[str, dict]) -> str:
    if not quotes:
        return "Chưa lấy được giá lúc này."
    return "\n".join(
        f"{symbol}: {fmt_price(q.get('price'))} VND ({fmt_pct(q.get('percent_change'), signed=True)}), "
        f"O/H/L {fmt_price(q.get('open'))}/{fmt_price(q.get('high'))}/{fmt_price(q.get('low'))}, "
        f"KL {fmt_price(q.get('volume'))} cp."
        for symbol, q in quotes.items()
    )


def market_summary(snapshot: dict) -> str:
    gate = (snapshot or {}).get("gate") or {}
    threshold = gate.get("buy_threshold")
    dist = gate.get("distribution_days")
    return (
        f"VN-Index đang ở trạng thái {gate.get('state', 'UNKNOWN')}; "
        f"{'không mở BUY mới' if threshold is None else f'BUY ≥ {threshold:g}'}. "
        f"Close {fmt_num(gate.get('close'))} so với MA50 {fmt_num(gate.get('ma50'))}; "
        f"phiên phân phối/25: {'N/A' if dist is None else dist}. {gate.get('reason') or ''}"
    ).strip()


def strategy_summary(rules: dict) -> str:
    return (
        "Strategy của bot chấm 100 điểm: T1 cơ bản 30, T2 sức mạnh/xu hướng 30, T3 breakout 40. "
        f"UPTREND cần ≥ {rules['buy_threshold_uptrend']:g} điểm; PRESSURE cần ≥ {rules['buy_threshold_pressure']:g} "
        "điểm. Breakout hợp lệ và hard filter đều là điều kiện bắt buộc, nên đủ điểm nhưng thiếu một trong các điều "
        "kiện này vẫn là HOLD. BUY/HOLD/SELL do rule engine quyết định; LLM chỉ diễn giải dữ liệu chứ không được "
        "đổi signal."
    )


# ---------------------------------------------------------------- kiểm tra chất lượng câu trả lời AI
GENERIC_AI_PHRASES = (
    "gui bao cao phan tich ma", "bao cao phan tich ma", "duoi day la bao cao",
    "fintech copilot gui", "toi se phan tich",
)
DECISION_TOKENS = ("buy", "hold", "sell", "/100", "t1", "t2", "t3", "breakout", "ma50", "roc63")


def ai_response_is_useful(text: str, intent: str, symbols: list[str]) -> bool:
    """Loại câu trả lời AI quá ngắn, chung chung hoặc không nhắc tới mã/số liệu nào."""
    t = str(text or "").strip()
    if len(t) < 70:
        return False
    if len(t) < 180 and contains_any(normalize_text(t), GENERIC_AI_PHRASES):
        return False
    if intent in {"analysis", "explain", "compare"}:
        low = t.lower()
        if symbols and not any(s.lower() in low for s in symbols):
            return False
        if not contains_any(low, DECISION_TOKENS):
            return False
    return True
