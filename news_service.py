"""Company news retrieval via Financial RSS feeds + Google News RSS + Groq analysis."""
from __future__ import annotations

import html
import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any

from dotenv import load_dotenv

from utils import normalize_text

load_dotenv()

# Tạo context bỏ qua lỗi chứng chỉ SSL trên Windows
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

RSS_SOURCES = {
    "CafeF": "https://cafef.vn/thi-truong-chung-khoan.rss",
    "Vietstock": "https://vietstock.vn/733/doanh-nghiep.rss",
}

COMPANY_KEYWORDS = {
    "PET": ["PET", "Petrosetco", "Dịch vụ Tổng hợp Dầu khí"],
    "FPT": ["FPT", "FPT Corporation", "FPT Telecom", "FPT Software"],
    "MWG": ["MWG", "Thế Giới Di Động", "Điện Máy Xanh", "Bách Hóa Xanh"],
    "VIC": ["VIC", "Vingroup", "Vincom", "Vinhomes", "VinFast"],
    "HPG": ["HPG", "Hòa Phát", "Tập đoàn Hòa Phát"],
    "SSI": ["SSI", "Chứng khoán SSI"],
    "VND": ["VND", "VNDirect", "Chứng khoán VNDirect"],
}

_NEWS_CACHE = {}
_NEWS_CACHE_LOCK = threading.Lock()
_NEWS_CACHE_TTL = 1800  # 30 phút

# RSS là dữ liệu dùng chung cho mọi mã. Bản cũ tải lại CafeF + Vietstock mỗi
# lần tìm một ticker khác, gây 2 request mạng thừa cho mỗi lần phân tích.
_RSS_CACHE = {"time": 0.0, "data": []}
_RSS_CACHE_LOCK = threading.Lock()
_RSS_CACHE_TTL = 300  # 5 phút, đủ mới nhưng tránh tải lặp liên tục

# Nội dung một bài báo và kết quả AI không cần crawl/phân tích lại mỗi 30 phút.
_ARTICLE_CACHE: dict[str, dict] = {}
_ARTICLE_CACHE_LOCK = threading.Lock()
_ARTICLE_CACHE_TTL = 1_800  # 30 phút, giữ cùng mức freshness với news cache
_AI_CACHE: dict[tuple[str, str], dict] = {}
_AI_CACHE_LOCK = threading.Lock()
_AI_CACHE_TTL = 1_800  # 30 phút


def clean_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return " ".join(text.split())


def parse_date(pub_date_str: str):
    if not pub_date_str:
        return None
    try:
        return parsedate_to_datetime(pub_date_str)
    except Exception:
        return None


def fetch_rss(url: str) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    )
    with urllib.request.urlopen(req, timeout=12, context=SSL_CTX) as response:
        return response.read()


def parse_rss(xml_data: bytes, source_name: str) -> list[dict]:
    root = ET.fromstring(xml_data)
    items = []
    for item in root.findall(".//item"):
        title = clean_html(item.findtext("title", default=""))
        link = item.findtext("link", default="").strip()
        pub_date = item.findtext("pubDate", default="").strip()
        desc = clean_html(item.findtext("description", default=""))
        if not title or not link:
            continue
        items.append({
            "title": title,
            "link": link,
            "published": pub_date,
            "datetime_obj": parse_date(pub_date),
            "description": desc,
            "source": source_name,
        })
    return items


def fetch_google_news_for_symbol(symbol: str, company_name: str = "") -> list[dict]:
    """Tìm kiếm trực tiếp các bài báo tài chính về mã cổ phiếu qua Google News RSS."""
    query = f"cổ phiếu {symbol}"
    if company_name:
        query += f" OR {company_name}"
    encoded_query = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={encoded_query}&hl=vi&gl=VN&ceid=VN:vi"
    
    try:
        xml_data = fetch_rss(url)
        return parse_rss(xml_data, "Báo chí tài chính")
    except Exception as e:
        print(f"[Google News Lỗi] {e}")
        return []


def get_all_rss_news() -> list[dict]:
    now = time.time()
    with _RSS_CACHE_LOCK:
        if _RSS_CACHE["data"] and now - _RSS_CACHE["time"] < _RSS_CACHE_TTL:
            return list(_RSS_CACHE["data"])
        stale = list(_RSS_CACHE["data"])

    def load_source(pair):
        src, url = pair
        try:
            return src, parse_rss(fetch_rss(url), src), None
        except Exception as exc:
            return src, [], exc

    all_news = []
    seen = set()
    sources = list(RSS_SOURCES.items())
    with ThreadPoolExecutor(max_workers=max(1, len(sources)), thread_name_prefix="news-rss") as pool:
        loaded = list(pool.map(load_source, sources))

    for src, parsed, error in loaded:
        if error is not None:
            print(f"[RSS Lỗi] {src}: {error}")
            continue
        for n in parsed:
            lnk = n["link"].strip().rstrip("/")
            if lnk not in seen:
                seen.add(lnk)
                all_news.append(n)

    # Nếu cả hai nguồn tạm lỗi, dùng bản RSS gần nhất thay vì vừa chậm vừa rỗng.
    if not all_news and stale:
        return stale

    with _RSS_CACHE_LOCK:
        _RSS_CACHE["time"] = now
        _RSS_CACHE["data"] = list(all_news)
    return all_news



def fetch_article_content(url: str) -> str:
    """Tự động cào các đoạn văn bản chính từ bài báo gốc để lấy số liệu thực tế."""
    if not url or not url.startswith("http"):
        return ""

    now = time.time()
    with _ARTICLE_CACHE_LOCK:
        cached = _ARTICLE_CACHE.get(url)
        if cached and now - cached["time"] < _ARTICLE_CACHE_TTL:
            return cached["content"]
        stale = cached["content"] if cached else ""

    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
        )
        with urllib.request.urlopen(req, timeout=5, context=SSL_CTX) as response:
            html_text = response.read().decode("utf-8", errors="ignore")
            # Trích xuất các thẻ đoạn văn <p>
            paragraphs = re.findall(r"<p\b[^>]*>(.*?)</p>", html_text, flags=re.IGNORECASE | re.DOTALL)
            clean_paras = []
            for p in paragraphs:
                cleaned = clean_html(p)
                if len(cleaned) > 40 and not any(kw in cleaned.lower() for kw in ["bản quyền", "liên hệ", "quảng cáo", "theo dõi"]):
                    clean_paras.append(cleaned)
            content = " ".join(clean_paras)
            content = content[:3500] if content else ""
            with _ARTICLE_CACHE_LOCK:
                _ARTICLE_CACHE[url] = {"time": now, "content": content}
            return content
    except Exception:
        return stale


def _fallback_ai_result(news_item: dict, reason: str = "Không thể phân tích tự động.") -> dict:
    return {
        "summary": news_item.get("description", ""),
        "sentiment": "Trung lập",
        "reason": reason,
    }


def _normalize_ai_result(res: dict, news_item: dict) -> dict:
    sent = str(res.get("sentiment") or "Trung lập")
    if sent not in {"Tích cực", "Trung lập", "Tiêu cực"}:
        sent = "Trung lập"
    return {
        "summary": str(res.get("summary") or news_item.get("description", "")),
        "sentiment": sent,
        "reason": str(res.get("reason") or ""),
    }


def _groq_json(prompt: str):
    """Một điểm gọi Groq dùng chung cho news; model nhanh + fallback ngắn."""
    try:
        from groq_service import FAST_MODEL, FALLBACK_MODEL, json_completion
        return json_completion(prompt, primary_model=FAST_MODEL,
                               fallback_model=FALLBACK_MODEL, max_tokens=1800)
    except Exception:
        return None

def analyze_with_groq(news_item: dict, symbol: str) -> dict:
    try:
        from groq_service import available as _groq_available
    except Exception:
        _groq_available = lambda: False
    if not _groq_available():
        return _fallback_ai_result(news_item, "Chưa cấu hình GROQ_API_KEY trong .env.")

    cache_key = (symbol.upper(), str(news_item.get("link") or news_item.get("title") or ""))
    now = time.time()
    with _AI_CACHE_LOCK:
        cached = _AI_CACHE.get(cache_key)
        if cached and now - cached["time"] < _AI_CACHE_TTL:
            return dict(cached["result"])

    # 1. Thử lấy nội dung bài gốc chi tiết
    url = news_item.get("link", "")
    full_content = fetch_article_content(url)
    
    # Nếu không cào được thì fallback về description
    article_body = full_content if len(full_content) > 150 else news_item.get("description", "")

    # 2. Prompt phân tích chuyên sâu chuẩn tài chính
    prompt = f"""
Bạn là chuyên gia phân tích tài chính và chứng khoán Việt Nam.
Hãy đọc kỹ nội dung bài viết dưới đây về mã cổ phiếu {symbol}:

TIÊU ĐỀ: {news_item.get("title", "")}
NỘI DUNG CHI TIẾT:
{article_body}

YÊU CẦU:
1. 'summary': Viết đoạn tóm tắt nội dung bài viết (3-5 câu mạch lạc). 
   - BẮT BUỘC trích xuất chính xác các số liệu kinh tế - tài chính cụ thể nếu có trong bài (ví dụ: số triệu cổ phiếu giao dịch, tỷ lệ sở hữu %, doanh thu, lợi nhuận sau thuế, % tăng trưởng YoY, kế hoạch kinh doanh, đối tác lớn...).
   - Đi thẳng vào sự kiện cốt lõi, tuyệt đối KHÔNG viết chung chung sáo rỗng kiểu "Bài viết cung cấp góc nhìn...", "Nội dung phản ánh...".
2. 'sentiment': Đánh giá đúng 1 trong 3 nhãn: 'Tích cực', 'Trung lập', hoặc 'Tiêu cực'.
3. 'reason': Nhận định tác động cụ thể đến cổ phiếu và tâm lý nhà đầu tư trong 2-3 câu ngắn gọn.

Chỉ trả về JSON hợp lệ:
{{
  "summary": "...",
  "sentiment": "Tích cực",
  "reason": "..."
}}
"""
    res = _groq_json(prompt)
    result = _normalize_ai_result(res, news_item) if isinstance(res, dict) else _fallback_ai_result(news_item)
    with _AI_CACHE_LOCK:
        _AI_CACHE[cache_key] = {"time": now, "result": dict(result)}
    return result


def analyze_news_batch_with_groq(news_items: list[dict], symbol: str) -> list[dict]:
    """Phân tích nhiều tin trong 1 request Groq thay vì 1 request/bài.

    Vẫn trả đủ summary/sentiment/reason cho từng bài. Nếu batch response thiếu
    một item, chỉ item đó mới fallback sang hàm phân tích đơn lẻ.
    """
    if not news_items:
        return []
    try:
        from groq_service import available as _groq_available
    except Exception:
        _groq_available = lambda: False
    if not _groq_available():
        return [_fallback_ai_result(x, "Chưa cấu hình GROQ_API_KEY trong .env.") for x in news_items]

    now = time.time()
    results: list[dict | None] = [None] * len(news_items)
    missing: list[int] = []
    for idx, item in enumerate(news_items):
        cache_key = (symbol.upper(), str(item.get("link") or item.get("title") or ""))
        with _AI_CACHE_LOCK:
            cached = _AI_CACHE.get(cache_key)
            if cached and now - cached["time"] < _AI_CACHE_TTL:
                results[idx] = dict(cached["result"])
                continue
        missing.append(idx)

    if not missing:
        return [x for x in results if x is not None]

    # Crawl bài gốc song song: worst-case 5 timeout không còn cộng thành 25s.
    with ThreadPoolExecutor(max_workers=min(5, len(missing)), thread_name_prefix="news-article") as pool:
        bodies = list(pool.map(lambda i: fetch_article_content(news_items[i].get("link", "")), missing))

    payload = []
    for idx, body in zip(missing, bodies):
        item = news_items[idx]
        article_body = body if len(body) > 150 else item.get("description", "")
        payload.append({
            "id": str(idx),
            "title": item.get("title", ""),
            "content": article_body,
        })

    prompt = f"""
Bạn là chuyên gia phân tích tài chính và chứng khoán Việt Nam.
Hãy phân tích các bài viết sau về mã cổ phiếu {symbol}.

DỮ LIỆU BÀI VIẾT:
{json.dumps(payload, ensure_ascii=False)}

Với MỖI bài, trả về:
- id: giữ nguyên id đầu vào.
- summary: 3-5 câu, đi thẳng vào sự kiện cốt lõi và giữ chính xác số liệu tài chính quan trọng nếu có.
- sentiment: đúng một trong 'Tích cực', 'Trung lập', 'Tiêu cực'.
- reason: 2-3 câu ngắn về tác động cụ thể đến cổ phiếu/tâm lý nhà đầu tư.

Chỉ trả về JSON hợp lệ theo đúng schema:
{{"items":[{{"id":"0","summary":"...","sentiment":"Tích cực","reason":"..."}}]}}
"""

    parsed = _groq_json(prompt)
    parsed_items = parsed.get("items", []) if isinstance(parsed, dict) else []
    by_id = {
        str(x.get("id")): x
        for x in parsed_items
        if isinstance(x, dict) and x.get("id") is not None
    }

    fallback_indices = []
    for idx in missing:
        raw = by_id.get(str(idx))
        if isinstance(raw, dict):
            result = _normalize_ai_result(raw, news_items[idx])
            results[idx] = result
            cache_key = (symbol.upper(), str(news_items[idx].get("link") or news_items[idx].get("title") or ""))
            with _AI_CACHE_LOCK:
                _AI_CACHE[cache_key] = {"time": now, "result": dict(result)}
        else:
            fallback_indices.append(idx)

    # Chỉ fallback per-item khi batch thiếu/không parse được, giữ độ tin cậy của
    # tính năng cũ mà không phải trả giá 5 Groq calls ở đường chạy bình thường.
    if fallback_indices:
        with ThreadPoolExecutor(max_workers=min(3, len(fallback_indices)), thread_name_prefix="news-ai-fallback") as pool:
            fallback_values = list(pool.map(lambda i: analyze_with_groq(news_items[i], symbol), fallback_indices))
        for idx, value in zip(fallback_indices, fallback_values):
            results[idx] = value

    return [x if x is not None else _fallback_ai_result(news_items[i]) for i, x in enumerate(results)]

def get_company_news(symbol: str, limit: int = 3, company_name: str = "") -> tuple[list[dict], Any, bool]:
    s_sym = symbol.upper().strip()
    key = (s_sym, limit, "v3_batch_content")
    now = time.time()

    with _NEWS_CACHE_LOCK:
        cached = _NEWS_CACHE.get(key)
        if cached and (now - cached["time"] < _NEWS_CACHE_TTL):
            return cached["data"]

    # 1. Lọc từ các trang RSS tài chính trong nước
    all_news = get_all_rss_news()
    keywords = list(COMPANY_KEYWORDS.get(s_sym, [s_sym]))
    if company_name and company_name not in keywords:
        keywords.append(company_name)

    matched = []
    for n in all_news:
        full_text = f"{n.get('title', '')} {n.get('description', '')}".casefold()
        if any(re.search(r"(?<!\w)" + re.escape(kw.casefold().strip()) + r"(?!\w)", full_text) for kw in keywords if kw.strip()):
            matched.append(n)

    # 2. Nếu không tìm thấy bài nào trên RSS trong ngày, tự động tra cứu báo chí qua Google News RSS
    if not matched:
        print(f"[Tin tức] Không có tin {s_sym} trên RSS hôm nay, đang tìm bài báo liên quan...")
        matched = fetch_google_news_for_symbol(s_sym, company_name)

    selected = matched[:limit]
    analyzed_news = []
    ai_results = analyze_news_batch_with_groq(selected, s_sym)
    for item, ai_res in zip(selected, ai_results):
        pub_str = item.get("published", "")
        if item.get("datetime_obj"):
            try:
                pub_str = item["datetime_obj"].strftime("%d/%m/%Y %H:%M")
            except Exception:
                pass

        analyzed_news.append({
            "title": item.get("title", ""),
            "summary": ai_res["summary"],
            "sentiment": ai_res["sentiment"],
            "sentiment_reason": ai_res["reason"],
            "publish_time": pub_str,
            "source": item.get("source", "Báo chí tài chính"),
            "url": item.get("link", ""),
            "provider": item.get("source", "Báo chí"),
        })

    result = (analyzed_news, None, False)
    with _NEWS_CACHE_LOCK:
        _NEWS_CACHE[key] = {"time": now, "data": result}

    return result