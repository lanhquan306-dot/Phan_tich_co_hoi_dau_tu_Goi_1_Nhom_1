"""Right-side chatbot grounded in the currently selected stock analysis.

With GROQ_API_KEY it uses the configured LLM. Without a key it still answers a
set of deterministic questions from the engine output. The AI never calculates
or overrides the official score/signal; it only explains the deterministic
analysis result.
"""
from __future__ import annotations

import json

import llm
from utils import normalize_text, to_float


def _fmt(v, digits=1):
    x = to_float(v)
    return "N/A" if x is None else f"{x:.{digits}f}"


def _fallback(question: str, result: dict) -> str:
    q = normalize_text(question)
    tech, fund, score = result["technical"], result["fundamental"], result["score"]
    signal = result.get("signal") or {}

    if any(k in q for k in ("mua", "ban", "giu", "buy", "sell", "hold", "tin hieu", "tai sao")):
        return (
            f"Tín hiệu hiện tại của {result['symbol']} là {signal.get('label','N/A')}. "
            f"Điểm tổng hợp {_fmt(score.get('score'))}/100; cơ bản {_fmt(score.get('fundamental',{}).get('score'))}/100; "
            f"kỹ thuật {_fmt(score.get('technical',{}).get('score'))}/100. Lý do: {signal.get('reason','Chưa có giải thích.')}"
        )
    if any(k in q for k in ("diem", "score", "danh gia")):
        return (
            f"{result['symbol']} đạt {_fmt(score.get('score'))}/100 theo công thức S = 0.6×F + 0.4×T. "
            f"F={_fmt(score.get('fundamental',{}).get('score'))}/100, T={_fmt(score.get('technical',{}).get('score'))}/100, "
            f"độ phủ dữ liệu {score.get('coverage_pct',0):.1f}%."
        )
    if any(k in q for k in ("ma20", "ma50", "rsi", "ky thuat", "xu huong", "khoi luong")):
        return (
            f"Xu hướng kỹ thuật: {tech.get('trend','N/A')}. Giá={_fmt(tech.get('close'),0)}, "
            f"MA20={_fmt(tech.get('ma20'),0)}, MA50={_fmt(tech.get('ma50'),0)}, RSI14={_fmt(tech.get('rsi14'))}, "
            f"khối lượng/TB20={_fmt(tech.get('vol_ratio'),2)}x. " + " ".join(tech.get("reasons") or [])
        )
    if any(k in q for k in ("rui ro", "risk")):
        return "Các rủi ro chính: " + ("; ".join(result.get("risks") or []) or "chưa có rủi ro nổi bật được engine gắn cờ từ dữ liệu hiện có.")
    if any(k in q for k in ("pe", "pb", "dinh gia")):
        c = fund.get("criteria") or {}
        pe, pb = (c.get("pe") or {}).get("value"), (c.get("pb") or {}).get("value")
        return (
            f"P/E={_fmt(pe,2)}x, P/B={_fmt(pb,2)}x. Hệ thống ưu tiên dữ liệu tham chiếu nếu cấu hình có sẵn; "
            "nếu không sẽ dùng dải định giá thử nghiệm. P/E thấp không tự động được xem là tốt."
        )
    if any(k in q for k in ("tang truong", "doanh thu", "loi nhuan", "lnst")):
        c = fund.get("criteria") or {}
        rg = (c.get("revenue_growth") or {}).get("value")
        pg = (c.get("profit_growth") or {}).get("value")
        comments = (fund.get("comments") or {}).get("growth") or []
        return f"Tăng trưởng doanh thu/thu nhập={_fmt(rg)}%, LNST={_fmt(pg)}%. " + " ".join(comments)
    if any(k in q for k in ("roe", "roa", "de", "don bay")):
        c = fund.get("criteria") or {}
        roe, roa, de = (c.get("roe") or {}).get("value"), (c.get("roa") or {}).get("value"), (c.get("de") or {}).get("value")
        if fund.get("company_type") != "regular":
            return f"ROE={_fmt(roe)}%, ROA={_fmt(roa)}%. D/E không được dùng để chấm {fund.get('company_type_label','doanh nghiệp tài chính')} vì đòn bẩy có đặc thù ngành."
        return f"ROE={_fmt(roe)}%, ROA={_fmt(roa)}%, D/E={_fmt(de,2)}x."
    if any(k in q for k in ("tin tuc", "news", "su kien")):
        news = result.get("news") or []
        if not news:
            return "Hiện hệ thống chưa lấy được tin doanh nghiệp từ VNStock Reference cho mã này. Tin tức không được tự bịa và chưa được đưa trực tiếp vào điểm BUY/HOLD/SELL."
        items = []
        for n in news[:5]:
            items.append(f"{n.get('publish_time','N/A')}: {n.get('title','')} ({n.get('source') or n.get('provider') or 'VNStock Reference'})")
        return "Tin gần đây: " + "; ".join(items)
    if any(k in q for k in ("doanh nghiep", "cong ty", "mo ta", "von hoa", "niem yet")):
        ci = result.get("company_info") or {}
        cap = ci.get("market_cap")
        cap_text = "N/A" if to_float(cap) is None else f"{to_float(cap):,.0f} VND"
        return (f"{result.get('company_name') or result['symbol']} thuộc ngành {result.get('industry') or 'N/A'}, sàn {result.get('exchange') or 'N/A'}. "
                f"Ngày niêm yết: {ci.get('listing_date') or 'N/A'}; vốn hóa ước tính: {cap_text}. "
                + (ci.get('profile') or "Nguồn hiện có chưa cung cấp mô tả chi tiết."))
    if any(k in q for k in ("nguon", "source", "kiem chung", "annual report", "bao cao thuong nien")):
        rows = result.get("data_sources") or []
        summary = "; ".join(f"{x.get('data')}: {x.get('source')} ({x.get('period')})" for x in rows[:8])
        return "Bảng nguồn dữ liệu của báo cáo: " + (summary or "chưa có dữ liệu nguồn.")
    if any(k in q for k in ("thieu du lieu", "du lieu", "coverage", "do phu")):
        missing = result.get("missing_data") or []
        return f"Độ phủ dữ liệu là {score.get('coverage_pct',0):.1f}%. " + ("Các mục đang thiếu: " + ", ".join(missing) if missing else "Không có tiêu chí chấm điểm chính bị thiếu.")
    return result.get("assessment") or "Chưa có đủ dữ liệu để trả lời câu hỏi này."


def answer(question: str, result: dict) -> str:
    question = str(question or "").strip()[:1200]
    if not question:
        return "Bạn có thể hỏi về tín hiệu BUY/HOLD/SELL, điểm 60/40, tăng trưởng, P/E, P/B, ROE/ROA, MA20/MA50, RSI14 hoặc rủi ro."
    if not llm._groq_available():
        return _fallback(question, result)

    compact = {
        "symbol": result.get("symbol"), "company_name": result.get("company_name"),
        "industry": result.get("industry"), "assessment": result.get("assessment"),
        "fundamental": result.get("fundamental"), "technical": result.get("technical"),
        "score": result.get("score"), "signal": result.get("signal"), "risk_level": result.get("risk_level"),
        "strengths": result.get("strengths"), "weaknesses": result.get("weaknesses"),
        "risks": result.get("risks"), "missing_data": result.get("missing_data"),
        "company_info": result.get("company_info"), "news": result.get("news"),
        "data_sources": result.get("data_sources"), "annual_report_source": result.get("annual_report_source"),
        "methodology": result.get("methodology"),
    }
    messages = [
        {
            "role": "system",
            "content": (
                "Bạn là trợ lý giải thích dashboard phân tích cổ phiếu. Chỉ dùng JSON engine cung cấp. "
                "Không tự bịa số liệu, không tự thay đổi điểm hoặc tín hiệu BUY/HOLD/SELL, không đưa khuyến nghị đầu tư cá nhân hóa. "
                "Giải thích ngắn gọn bằng tiếng Việt, nêu dữ liệu thiếu và giới hạn khi liên quan."
            ),
        },
        {"role": "user", "content": "Dữ liệu engine:\n" + json.dumps(compact, ensure_ascii=False, default=str) + "\n\nCâu hỏi: " + question},
    ]
    try:
        res = llm.chat_completion(messages, temperature=.1, max_tokens=500)
        text = (res.choices[0].message.content or "").strip()
        return text or _fallback(question, result)
    except Exception as exc:
        llm._record_groq_error(exc)
        return _fallback(question, result)
