"""Lớp hội thoại: điều phối câu hỏi tới engine và (tùy chọn) dùng Groq để viết câu trả lời.

Nguyên tắc: engine (stock_service + strategy) là nguồn sự thật duy nhất cho giá,
chỉ báo, điểm và BUY/HOLD/SELL. Groq KHÔNG được tính lại hay đổi các giá trị đó.

Luồng xử lý một câu chat (run_financial_agent):
    1. conversation.classify_intent + contextual_symbols -> biết ý định và mã.
    2. Ý định cổ phiếu (quote / analysis / compare / explain / market / strategy):
       engine tính -> conversation viết bản trả lời chuẩn -> Groq làm mượt tối đa 1 lần.
       Groq lỗi hoặc viết chung chung thì giữ bản chuẩn.
    3. Câu hỏi tài chính mở (general): Groq tự chọn công cụ (tool calling) để lấy dữ liệu engine.

Khi Groq báo lỗi xác thực/quota, "cầu dao" (circuit breaker) ngắt các lượt gọi
tiếp theo một thời gian để không phí thời gian chờ.
"""
from __future__ import annotations

import contextlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import conversation as conv
from config import GROQ_API_KEY, GROQ_MODEL, GROQ_RETRY_ATTEMPTS, GROQ_TIMEOUT_MS, STRATEGY
from utils import to_float

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
MAX_TOOL_ROUNDS = 4

_client = None
_blocked_until = 0.0
_block_reason: str | None = None


# =====================================================================
# Groq client + cầu dao
# =====================================================================
def get_client():
    """Client OpenAI-compatible trỏ tới Groq; None nếu chưa cấu hình API key."""
    global _client
    if not GROQ_API_KEY:
        return None
    if _client is None:
        from openai import OpenAI
        _client = OpenAI(api_key=GROQ_API_KEY, base_url=GROQ_BASE_URL,
                         timeout=GROQ_TIMEOUT_MS / 1000.0, max_retries=GROQ_RETRY_ATTEMPTS)
    return _client


def groq_extra_body() -> dict:
    """Model GPT-OSS là model suy luận: đặt reasoning thấp và ẩn phần suy luận để
    không tiêu hết token trước khi viết câu trả lời. Model khác không cần."""
    if GROQ_MODEL.lower().startswith("openai/gpt-oss-"):
        return {"reasoning_effort": "low", "include_reasoning": False}
    return {}


def _groq_available() -> bool:
    return get_client() is not None and time.time() >= _blocked_until


def _record_groq_error(exc: Exception) -> str:
    """Mở cầu dao 10 phút khi sai key/bị từ chối, 90 giây khi hết quota."""
    global _blocked_until, _block_reason
    msg = f"{type(exc).__name__}: {str(exc)[:300]}"
    low = msg.lower()
    if any(k in low for k in ("authentication", "invalid api key", "invalid_api_key", "401",
                              "permission_denied", "denied access", "403")):
        _blocked_until, _block_reason = time.time() + 600, msg
    elif any(k in low for k in ("rate limit", "quota", "429")):
        _blocked_until, _block_reason = time.time() + 90, msg
    return msg


def chat_completion(messages: list[dict], *, temperature: float = 0.15, max_tokens: int = 1024, **kwargs):
    return get_client().chat.completions.create(
        model=GROQ_MODEL, messages=messages, temperature=temperature,
        max_completion_tokens=max_tokens, extra_body=groq_extra_body(), **kwargs,
    )


# =====================================================================
# Dữ liệu engine gửi cho Groq (chỉ số liệu, dạng JSON gọn)
# =====================================================================
def _tier_context(tier: dict | None) -> dict:
    tier = tier or {}
    return {
        "score": tier.get("score"), "score_min": tier.get("score_min"),
        "score_max": tier.get("score_max"), "max": tier.get("max"),
        "components": {
            code: {k: item.get(k) for k in ("points", "max", "value", "detail")}
            for code, item in (tier.get("components") or {}).items()
        },
    }


def build_stock_context(result: dict) -> dict:
    """Ảnh chụp kết quả analyze_symbol() để Groq diễn giải, không có gì ngoài output của engine."""
    q, tech, fund, market = (result.get(k) or {} for k in ("quote", "tech", "fund", "market"))
    return {
        "symbol": result.get("symbol"),
        "company_name": result.get("company_name"),
        "exchange": result.get("exchange"),
        "data_dates": {
            "stock_eod": result.get("confirmed_bar_date"),
            "vnindex_eod": result.get("index_bar_date"),
            "quote_retrieved_at": q.get("retrieved_at"),
            "fundamental_period": fund.get("latest_period"),
        },
        "quote": build_quote_context(result.get("symbol"), q) | {
            "source": result.get("price_source") or q.get("data_source")},
        "decision": {
            "signal": result.get("signal"),
            "hold_type": result.get("hold_type"),
            "score": result.get("score"),
            "score_min": result.get("score_min"),
            "score_max": result.get("score_max"),
            "score_complete": result.get("score_complete"),
            "buy_threshold": result.get("threshold"),
            "position_weight": result.get("weight"),
            "buy_zone": to_float(result.get("buy_zone")),
            "breakout_pivot": to_float(result.get("breakout_pivot")),
            "stop_reference": to_float(result.get("stop_ref")),
            "reasons": result.get("reasons") or [],
            "checks": result.get("checks") or {},
            "hard_filter": result.get("hard") or {},
        },
        "market": {
            "state": market.get("state"),
            "vnindex_close": to_float(market.get("close")),
            "ma50": to_float(market.get("ma50")),
            "distribution_days": market.get("distribution_days"),
            "allow_buy": market.get("allow_buy"),
            "buy_threshold": market.get("buy_threshold"),
            "reason": market.get("reason"),
        },
        "fundamental": {
            "source": fund.get("fund_source"),
            "eps_yoy_pct": to_float(fund.get("eps_yoy")),
            "eps_status": fund.get("eps_status"),
            "roe_ttm_pct": to_float(fund.get("roe")),
            "debt_equity": to_float(fund.get("debt_equity")),
            "is_financial_company": fund.get("is_financial"),
        },
        "technical": {
            "close": to_float(tech.get("close")),
            "ma20": to_float(tech.get("ma20")),
            "ma50": to_float(tech.get("ma50")),
            "ma150": to_float(tech.get("ma150")),
            "rsi14": to_float(tech.get("rsi14")),
            "volume_ratio_3d": to_float(tech.get("vol_ratio_3d")),
            "high52_ratio": to_float(tech.get("high52_ratio")),
            "roc63_pct": to_float(tech.get("roc63")),
            "vnindex_roc63_pct": to_float(tech.get("index_roc63")),
            "delta_roc63_pct_point": to_float(tech.get("delta_roc63")),
            "breakout": tech.get("breakout") or {},
        },
        "tiers": {name: _tier_context(result.get(key))
                  for name, key in (("T1", "tier1"), ("T2", "tier2"), ("T3", "tier3"))},
        "warnings": result.get("warnings") or [],
        "errors": result.get("errors") or [],
    }


def build_quote_context(symbol: str | None, q: dict) -> dict:
    return {
        "symbol": symbol,
        **{k: to_float(q.get(k)) for k in ("price", "reference", "open", "high", "low", "volume", "percent_change")},
        "retrieved_at": q.get("retrieved_at"),
        "source": q.get("data_source"),
        "stale_cache": bool(q.get("stale_cache")),
    }


def strategy_context() -> dict:
    return {
        "total_score": 100,
        "tiers": {"T1": 30, "T2": 30, "T3": 40},
        "buy_threshold_uptrend": STRATEGY.buy_score_uptrend,
        "buy_threshold_pressure": STRATEGY.buy_score_pressure,
        "breakout_required_for_buy": True,
        "position_weight_uptrend": STRATEGY.position_weight,
        "position_weight_pressure": STRATEGY.position_weight * STRATEGY.pressure_weight_factor,
        "market_states": ["UPTREND", "PRESSURE", "WEAK", "DOWNTREND", "UNKNOWN"],
        "note": "BUY/HOLD/SELL is decided by deterministic strategy.py; the LLM must not override it.",
    }


def _history_text(history: list[dict] | None) -> str:
    lines = []
    for item in (history or [])[-8:]:
        text = str(item.get("text", "")).strip().replace("\n", " ")
        if text:
            lines.append(f"{'USER' if item.get('role') == 'user' else 'ASSISTANT'}: {text[:700]}")
    return "\n".join(lines) or "(không có)"


def _to_json(data) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


# =====================================================================
# Kết quả một lượt hội thoại
# =====================================================================
@dataclass
class AgentReply:
    text: str = ""
    mode: str = "general"          # analysis / comparison / explain / quote / market / general
    analyses: dict[str, dict] = field(default_factory=dict)
    quotes: dict[str, dict] = field(default_factory=dict)
    market: dict | None = None
    tool_trace: list[str] = field(default_factory=list)   # các bước đã chạy, để debug/log
    error: str | None = None


def _join_errors(*parts) -> str | None:
    items = [p for p in parts if p]
    return " | ".join(items) or None


# =====================================================================
# Gọi engine
# =====================================================================
def _analyze_many(service, symbols: list[str], positions: dict[str, dict],
                  trace: list[str]) -> tuple[dict[str, dict], list[str]]:
    """Phân tích song song nhiều mã, giữ đúng thứ tự người dùng nhắc tới."""
    # Nạp trước danh sách mã và VN-Index (dùng chung) để các thread không cùng tải lại.
    for warm_up in (service.listing_map, service.get_market_gate):
        with contextlib.suppress(Exception):
            warm_up()

    def run(symbol):
        try:
            return symbol, service.analyze_symbol(symbol, positions.get(symbol)), None
        except Exception as exc:
            return symbol, None, f"{symbol}: {type(exc).__name__}: {str(exc)[:160]}"

    results, failures = {}, []
    with ThreadPoolExecutor(max_workers=min(4, max(1, len(symbols)))) as pool:
        for symbol, result, error in pool.map(run, symbols):
            if error:
                failures.append(error)
                trace.append(f"analyze_failed({symbol})")
            else:
                results[symbol] = result
                trace.append(f"analyze({symbol})")
    return results, failures


def _fetch_quotes(service, symbols: list[str], trace: list[str]) -> tuple[dict[str, dict], list[str]]:
    quotes, failures = {}, []
    for symbol in symbols:
        try:
            q, _, stale = service.get_quote(symbol)
            quotes[symbol] = dict(q, stale_cache=bool(stale))
            trace.append(f"quote({symbol})")
        except Exception as exc:
            failures.append(f"{symbol}: {type(exc).__name__}: {str(exc)[:160]}")
    return quotes, failures


def _market_snapshot(service) -> dict:
    idx, gate, stale = service.get_market_gate()
    return {"gate": gate, "bar_date": service.bar_date(idx), "stale_cache": bool(stale)}


# =====================================================================
# Các luồng trả lời theo quy tắc (+ làm mượt bằng Groq nếu có)
# =====================================================================
POLISH_PROMPT = """
Bạn là FINTECH COPILOT cho cổ phiếu Việt Nam. Hãy làm câu trả lời tự nhiên hơn nhưng KHÔNG thay đổi
BUY/HOLD/SELL, điểm, chỉ báo, ngày hoặc bất kỳ fact nào từ ENGINE_DATA. LOCAL_DRAFT đã được rule engine
tạo đúng; dùng nó làm xương sống. Trả lời trực tiếp câu hỏi, có kết luận và bằng chứng cụ thể.
Không được trả lời chung chung kiểu 'gửi báo cáo phân tích mã'. Không chain-of-thought, không bảng Markdown.
Tối đa 220 từ, ưu tiên bullet •.

INTENT: {intent}
HISTORY:
{history}
QUESTION:
{question}
LOCAL_DRAFT:
{draft}
ENGINE_DATA:
{data}
""".strip()


def _polish(question: str, intent: str, analyses: dict[str, dict], draft: str,
            history: list[dict] | None, trace: list[str]) -> tuple[str, str | None]:
    """Nhờ Groq viết lại bản nháp (tối đa 1 lần gọi). Trả về (câu trả lời, lỗi nếu có)."""
    if get_client() is None:
        return draft, "Groq chưa được cấu hình"
    if not _groq_available():
        trace.append("groq_circuit_open")
        return draft, _block_reason or "Groq tạm thời bị ngắt"

    prompt = POLISH_PROMPT.format(
        intent=intent, history=_history_text(history), question=question, draft=draft,
        data=_to_json([build_stock_context(r) for r in analyses.values()]),
    )
    try:
        response = chat_completion([{"role": "user", "content": prompt}], temperature=0.10)
    except Exception as exc:
        trace.append(f"groq_unavailable({type(exc).__name__})")
        return draft, _record_groq_error(exc)
    text = (response.choices[0].message.content or "").strip()
    if conv.ai_response_is_useful(text, intent, list(analyses)):
        trace.append("groq_polish_ok")
        return text, None
    trace.append("groq_polish_rejected")
    return draft, "Groq trả lời quá chung chung nên bot dùng bản của engine"


def _stock_turn(question, symbols, history, positions, service, intent: str) -> AgentReply:
    """Phân tích một mã hoặc so sánh nhiều mã."""
    trace = [f"intent({intent})"]
    analyses, failures = _analyze_many(service, symbols, positions, trace)
    if not analyses:
        return AgentReply(text="Không lấy được dữ liệu phân tích cho mã vừa hỏi. " + "; ".join(failures),
                          error="analysis_failed", tool_trace=trace)
    if intent == "compare" and len(analyses) >= 2:
        draft, mode = conv.comparison_summary(analyses), "comparison"
    else:
        draft, mode = conv.single_analysis_summary(next(iter(analyses.values()))), "analysis"
    text, ai_error = _polish(question, intent, analyses, draft, history, trace)
    return AgentReply(text=text, mode=mode, analyses=analyses, tool_trace=trace,
                      error=_join_errors("; ".join(failures), ai_error))


def _explain_turn(question, symbols, history, previous_results, positions, service) -> AgentReply:
    """Câu hỏi nối tiếp ('tại sao?', 'T3 là gì?'): ưu tiên dùng lại kết quả cũ, không gọi VNStock."""
    trace = ["intent(explain)"]
    analyses: dict[str, dict] = {}
    if conv.should_reuse_previous(question, "explain"):
        for symbol in symbols:
            if symbol in previous_results:
                analyses[symbol] = previous_results[symbol]
                trace.append(f"reuse_analysis({symbol})")
    failures: list[str] = []
    missing = [s for s in symbols if s not in analyses]
    if missing:
        fresh, failures = _analyze_many(service, missing, positions, trace)
        analyses.update(fresh)
    if not analyses:
        return AgentReply(text="Mình chưa có phân tích nào trước đó để giải thích. "
                               "Hãy nêu mã, ví dụ: “tại sao OCB chỉ 30 điểm?”.", tool_trace=trace)

    if len(analyses) >= 2:
        draft, mode = conv.comparison_summary(analyses), "comparison"
    else:
        draft, mode = conv.explanation_summary(next(iter(analyses.values())), question), "explain"
    text, ai_error = _polish(question, "explain", analyses, draft, history, trace)
    return AgentReply(text=text, mode=mode, analyses=analyses, tool_trace=trace,
                      error=_join_errors("; ".join(failures), ai_error))


def _quote_turn(symbols: list[str], service) -> AgentReply:
    trace = ["intent(quote)"]
    quotes, failures = _fetch_quotes(service, symbols, trace)
    return AgentReply(text=conv.quote_summary(quotes), mode="quote" if quotes else "general",
                      quotes=quotes, tool_trace=trace, error=_join_errors("; ".join(failures)))


def _market_turn(service) -> AgentReply:
    trace = ["intent(market)"]
    try:
        snapshot = _market_snapshot(service)
    except Exception as exc:
        return AgentReply(text=f"Chưa lấy được trạng thái VN-Index lúc này: {type(exc).__name__}.",
                          tool_trace=trace, error=f"{type(exc).__name__}: {str(exc)[:220]}")
    return AgentReply(text=conv.market_summary(snapshot), mode="market", market=snapshot, tool_trace=trace)


# =====================================================================
# Câu hỏi mở: Groq tự gọi công cụ (tool calling)
# =====================================================================
SYSTEM_PROMPT = """
Bạn là FINTECH COPILOT, chatbot tài chính hội thoại cho cổ phiếu Việt Nam HOSE/HNX.
Bạn phải hiểu ý định, chọn đúng tool, lấy đủ dữ liệu rồi mới trả lời.

QUY TẮC TOOL-CALLING:
- Câu hỏi về "có nên mua", BUY/HOLD/SELL, phân tích, breakout, điểm, cơ bản/kỹ thuật -> gọi analyze_stocks.
- So sánh 2 hay nhiều mã -> gọi analyze_stocks với TOÀN BỘ ticker trong cùng một lần gọi.
- Chỉ hỏi giá/OHLC/khối lượng -> gọi get_stock_quotes.
- VN-Index/trạng thái thị trường -> gọi get_market_snapshot.
- Quy tắc T1/T2/T3/ngưỡng BUY của bot -> gọi get_strategy_rules.
- Kiến thức tài chính chung không phụ thuộc dữ liệu hiện tại có thể trả lời trực tiếp.

GROUNDING BẮT BUỘC:
- Kết quả tool là nguồn sự thật duy nhất cho giá, ngày, chỉ báo, điểm, market gate và signal.
- Không tự tính lại hoặc thay BUY/HOLD/SELL. Không bịa tin tức, BCTC hoặc chỉ báo không có trong tool result.
- Dữ liệu thiếu, UNKNOWN, warning/error: nói rõ và giảm độ chắc chắn.
- Khi so sánh, chỉ nói mã nào PHÙ HỢP HƠN VỚI CHIẾN LƯỢC này; nếu không mã nào BUY thì nói rõ, không ép chọn.
- Không suy diễn mức phù hợp theo hồ sơ cá nhân khi người dùng chưa cung cấp mục tiêu/rủi ro.

CÁCH TRẢ LỜI:
- Tiếng Việt tự nhiên, kết luận ngắn trước, sau đó 3-6 ý quan trọng nhất.
- Không dùng bảng Markdown, **bold** hay # heading. Không hiển thị chain-of-thought. Không lặp disclaimer dài.
""".strip()


def _tool_schema(name: str, description: str, with_symbols: bool) -> dict:
    params: dict = {"type": "object", "properties": {}, "additionalProperties": False}
    if with_symbols:
        params["properties"]["symbols"] = {"type": "array", "items": {"type": "string"},
                                           "minItems": 1, "maxItems": 5,
                                           "description": "HOSE/HNX tickers; include every ticker being compared."}
        params["required"] = ["symbols"]
    return {"type": "function", "function": {"name": name, "description": description, "parameters": params}}


TOOL_SCHEMAS = [
    _tool_schema("analyze_stocks", "Analyze 1-5 Vietnamese tickers with the deterministic strategy engine.", True),
    _tool_schema("get_stock_quotes", "Get the latest quote data for 1-5 Vietnamese tickers.", True),
    _tool_schema("get_market_snapshot", "Get the deterministic VN-Index market gate used by the strategy.", False),
    _tool_schema("get_strategy_rules", "Return the deterministic T1/T2/T3 scoring and market-gate rules.", False),
]


def _clean_symbols(symbols, limit: int = 5) -> list[str]:
    """Làm sạch danh sách mã do LLM gửi (có thể là chuỗi, có ký tự lạ...)."""
    if isinstance(symbols, str):
        symbols = [symbols]
    out: list[str] = []
    for raw in symbols if isinstance(symbols, (list, tuple)) else []:
        s = re.sub(r"[^A-Za-z0-9]", "", str(raw or "")).upper()
        if 2 <= len(s) <= 6 and s not in out:
            out.append(s)
    return out[:limit]


class _EngineTools:
    """Công cụ Groq được phép gọi. Lưu lại kết quả đầy đủ để bot hiển thị thẻ số liệu."""

    def __init__(self, service, positions: dict[str, dict], trace: list[str]):
        self.service, self.positions, self.trace = service, positions, trace
        self.analyses: dict[str, dict] = {}
        self.quotes: dict[str, dict] = {}
        self.market: dict | None = None

    def _resolve(self, symbols) -> tuple[list[str], list[dict]]:
        resolved, failures = [], []
        for raw in _clean_symbols(symbols):
            res = self.service.resolve_symbol(raw)
            if res.get("status") == "ok":
                resolved.append(res["symbol"])
            else:
                failures.append({"input": raw, "error": "ticker_not_resolved", "resolution": res})
        return resolved, failures

    def analyze_stocks(self, symbols) -> dict:
        resolved, failures = self._resolve(symbols)
        self.trace.append(f"analyze_stocks({','.join(resolved)})")
        results, errors = _analyze_many(self.service, resolved, self.positions, self.trace)
        self.analyses.update(results)
        return {"stocks": [build_stock_context(r) for r in results.values()], "failures": failures + errors}

    def get_stock_quotes(self, symbols) -> dict:
        resolved, failures = self._resolve(symbols)
        quotes, errors = _fetch_quotes(self.service, resolved, self.trace)
        self.quotes.update(quotes)
        return {"quotes": [build_quote_context(s, q) for s, q in quotes.items()], "failures": failures + errors}

    def get_market_snapshot(self) -> dict:
        self.trace.append("get_market_snapshot()")
        self.market = _market_snapshot(self.service)
        return self.market

    def get_strategy_rules(self) -> dict:
        self.trace.append("get_strategy_rules()")
        return strategy_context()

    def call(self, name: str, arguments: str) -> dict:
        fn = getattr(self, name, None) if name in {s["function"]["name"] for s in TOOL_SCHEMAS} else None
        if fn is None:
            return {"error": f"unknown_tool:{name}"}
        try:
            return fn(**json.loads(arguments or "{}"))
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {str(exc)[:220]}"}


def _open_question_turn(question, history, previous_results, mentioned, positions, service,
                        trace: list[str]) -> AgentReply:
    """Vòng tool calling chuẩn OpenAI: model yêu cầu tool -> chạy tool -> trả kết quả -> lặp tối đa 4 vòng."""
    if not _groq_available():
        return AgentReply(
            text=("Phần dữ liệu cổ phiếu vẫn hoạt động bình thường, nhưng câu hỏi mở này cần lớp LLM và Groq "
                  "hiện không khả dụng. Bạn vẫn có thể hỏi giá, phân tích một mã, so sánh mã, breakout, "
                  "T1/T2/T3 hoặc trạng thái VN-Index."),
            error=_block_reason or "missing_groq_api_key", tool_trace=trace,
        )

    recent = {s: build_stock_context(r) for s, r in list(previous_results.items())[-3:]}
    prompt = (
        f"CONVERSATION HISTORY:\n{_history_text(history)}\n\n"
        f"PRIOR ENGINE SNAPSHOTS FROM THIS CHAT:\n{_to_json(recent) if recent else '(không có)'}\n\n"
        f"DETERMINISTICALLY DETECTED TICKERS:\n{', '.join(mentioned) or '(none)'}\n\n"
        f"CURRENT USER QUESTION:\n{question}\n\n"
        "Resolve the user's intent. Use tools whenever current market/stock facts are needed, "
        "then answer the actual question."
    )
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}]
    tools = _EngineTools(service, positions, trace)
    try:
        response = chat_completion(messages, max_tokens=1400, tools=TOOL_SCHEMAS, tool_choice="auto")
        for _ in range(MAX_TOOL_ROUNDS):
            msg = response.choices[0].message
            if not msg.tool_calls:
                break
            messages.append({"role": "assistant", "content": msg.content or "", "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ]})
            for tc in msg.tool_calls:
                messages.append({"role": "tool", "tool_call_id": tc.id, "name": tc.function.name,
                                 "content": _to_json(tools.call(tc.function.name, tc.function.arguments))})
            response = chat_completion(messages, max_tokens=1400, tools=TOOL_SCHEMAS, tool_choice="auto")
        if response.choices[0].message.tool_calls:
            # Hết số vòng cho phép: buộc model viết câu trả lời từ dữ liệu đã có.
            response = chat_completion(messages, temperature=0.10, max_tokens=1400,
                                       tools=TOOL_SCHEMAS, tool_choice="none")
        text = (response.choices[0].message.content or "").strip()
    except Exception as exc:
        return AgentReply(text=conv.FALLBACK_TEXT, analyses=tools.analyses, quotes=tools.quotes,
                          market=tools.market, tool_trace=trace, error=_record_groq_error(exc))

    if tools.analyses:
        mode = "comparison" if len(tools.analyses) >= 2 else "analysis"
    elif tools.quotes:
        mode = "quote"
    elif tools.market is not None:
        mode = "market"
    else:
        mode = "general"
    return AgentReply(text=text, mode=mode, analyses=tools.analyses, quotes=tools.quotes,
                      market=tools.market, tool_trace=trace)


# =====================================================================
# Điểm vào chính
# =====================================================================
def run_financial_agent(question: str, history: list[dict] | None = None,
                        previous_results: dict[str, dict] | None = None,
                        positions: dict[str, dict] | None = None,
                        recent_symbols: list[str] | None = None) -> AgentReply:
    """Xử lý một câu chat.

    history          : vài lượt hội thoại gần nhất [{role, text}].
    previous_results : kết quả analyze_symbol của các mã vừa hỏi (bộ nhớ ngắn hạn).
    positions        : vị thế mô phỏng của người dùng {mã: {entry_price, ...}}.
    recent_symbols   : các mã vừa được nhắc tới (kể cả khi chỉ hỏi giá).
    """
    import stock_service as service  # import muộn để selftests có thể thay thế hàm của service

    previous_results = previous_results or {}
    positions = {str(k).upper(): v for k, v in (positions or {}).items()}
    # Mã chỉ được hỏi giá cũng cần để hiểu "nó" ở câu sau; dùng bản giữ chỗ.
    symbol_context = dict(previous_results)
    for s in (recent_symbols or [])[-5:]:
        symbol_context.setdefault(str(s).upper(), {"symbol": str(s).upper()})

    explicit = service.resolve_symbols(question, max_symbols=5)
    intent = conv.classify_intent(question, explicit, symbol_context)
    symbols = conv.contextual_symbols(question, explicit, symbol_context, max_symbols=5)
    trace = [f"planner({intent})"]

    if intent == "quote":
        return (_quote_turn(symbols, service) if symbols else
                AgentReply(text="Bạn muốn xem giá mã nào? Ví dụ: “giá OCB hiện tại”.", tool_trace=trace))
    if intent == "market":
        return _market_turn(service)
    if intent == "strategy":
        return AgentReply(text=conv.strategy_summary(strategy_context()), tool_trace=trace)
    if intent == "explain":
        if symbols:
            return _explain_turn(question, symbols, history, previous_results, positions, service)
        return AgentReply(text="Mình chưa có mã nào trong ngữ cảnh để giải thích. "
                               "Hãy nêu mã, ví dụ: “tại sao OCB chưa BUY?”.", tool_trace=trace)
    if intent == "compare" and len(symbols) == 1:
        return AgentReply(text=f"Mình đã nhận {symbols[0]}, nhưng chưa xác định được mã còn lại để so sánh. "
                               "Ví dụ: “OCB so với VIC”.", tool_trace=trace)
    if intent in {"compare", "analysis"} and symbols:
        return _stock_turn(question, symbols, history, positions, service, intent)

    return _open_question_turn(question, history, previous_results, symbols, positions, service, trace)


def explain_stock(question: str, result: dict, history: list[dict] | None = None) -> str:
    """Đoạn giải thích bằng AI gửi kèm lệnh /signal. Trả chuỗi rỗng nếu Groq không dùng được."""
    if not _groq_available():
        return ""
    prompt = (
        "Bạn là trợ lý tài chính Việt Nam. ENGINE_DATA là nguồn sự thật duy nhất.\n"
        "Không sửa signal/score, không bịa dữ liệu. Trả lời trực tiếp câu hỏi, nêu kết luận trước,\n"
        "sau đó giải thích 3-5 yếu tố quan trọng nhất. Không hiển thị chain-of-thought.\n\n"
        f"CONVERSATION:\n{_history_text(history)}\n\nQUESTION:\n{question}\n\n"
        f"ENGINE_DATA:\n{json.dumps(build_stock_context(result), ensure_ascii=False, indent=2, default=str)}"
    )
    try:
        response = chat_completion([{"role": "user", "content": prompt}], max_tokens=900)
    except Exception as exc:
        _record_groq_error(exc)
        return ""
    return (response.choices[0].message.content or "").strip()
