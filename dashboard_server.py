"""HTTP server for the stock opportunity-analysis website.

Routes
------
GET  /                    landing page / ticker search
GET  /analysis/<SYMBOL>   full analysis dashboard
GET  /chart/<SYMBOL>?p=6M legacy interactive technical chart
GET  /report/<SYMBOL>.pdf on-demand PDF report
GET  /api/analysis/<SYMBOL> JSON result for integrations (TV1/TV4)
GET  /api/backtest/<SYMBOL>?months=12 historical rule validation, 1 năm (years=1 legacy)
GET  /api/validate?symbols=FPT,VCB,... multi-symbol QA table
GET  /api/collect?symbols=FPT,VCB,... independent multi-symbol raw collector
POST /api/chat             small right-side chatbot grounded in selected result
GET  /healthz              health check

The project deliberately keeps the standard-library HTTP server so the old
Telegram process can host the website without adding Flask/FastAPI.
"""
from __future__ import annotations

import html
import json
import logging
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import chart_service as charts
import investment_analysis as investment
import investment_chat
import report_pdf_new as report_pdf
import opportunity_backtest
import stock_service as service
import multi_stock_validation
import multi_stock_collector
import json
import multi_stock_validation as validator

log = logging.getLogger("fintech-dashboard")
BASE_DIR = Path(__file__).resolve().parent
INVESTMENT_TEMPLATE = BASE_DIR / "templates" / "investment_dashboard.html"

SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,10}$")
PAGE_CACHE_SECONDS = 60
_page_cache: dict[tuple[str, str], tuple[float, str]] = {}
_analysis_cache: dict[str, tuple[float, dict]] = {}
_analysis_inflight: dict[str, threading.Event] = {}
_pdf_cache: dict[str, tuple[float, bytes]] = {}
_pdf_inflight: dict[str, threading.Event] = {}
PDF_CACHE_SECONDS = 300
REPORT_ANALYSIS_REUSE_SECONDS = 900
_cache_lock = threading.Lock()


def _safe_json(data) -> str:
    return json.dumps(data, ensure_ascii=False, default=str, separators=(",", ":")).replace("</", "<\\/")


def _analysis(symbol: str) -> dict:
    now = time.time()
    with _cache_lock:
        hit = _analysis_cache.get(symbol)
        if hit and now - hit[0] <= PAGE_CACHE_SECONDS:
            return hit[1]
        event = _analysis_inflight.get(symbol)
        if event is None:
            event = threading.Event()
            _analysis_inflight[symbol] = event
            owner = True
        else:
            owner = False

    # Nếu browser/API/PDF cùng hỏi một mã đúng lúc phân tích đang chạy, dùng
    # chung kết quả thay vì khởi động thêm một pipeline VNStock + Groq nữa.
    if not owner:
        event.wait()
        with _cache_lock:
            hit = _analysis_cache.get(symbol)
            if hit and time.time() - hit[0] <= PAGE_CACHE_SECONDS:
                return hit[1]

    try:
        result = investment.analyze_investment(symbol)
    except Exception:
        if owner:
            with _cache_lock:
                _analysis_inflight.pop(symbol, None)
                event.set()
        raise

    completed_at = time.time()
    with _cache_lock:
        # TTL phải tính từ lúc pipeline hoàn tất, không phải lúc bắt đầu.
        # Nếu một lượt phân tích mất >60s, cách cũ khiến cache vừa tạo đã hết hạn.
        _analysis_cache[symbol] = (completed_at, result)
        for key, (ts, _) in list(_analysis_cache.items()):
            if completed_at - ts > PAGE_CACHE_SECONDS:
                _analysis_cache.pop(key, None)
        if owner:
            _analysis_inflight.pop(symbol, None)
            event.set()
    return result



def _analysis_for_report(symbol: str) -> dict:
    """PDF phải dùng đúng snapshot dashboard gần nhất thay vì chạy lại pipeline AI.

    Dashboard vẫn có TTL ngắn 60s để người dùng chủ động refresh dữ liệu; riêng
    export được phép reuse snapshot 15 phút để báo cáo khớp màn hình và xuất nhanh.
    """
    now=time.time()
    with _cache_lock:
        hit=_analysis_cache.get(symbol)
        if hit and now-hit[0] <= REPORT_ANALYSIS_REUSE_SECONDS:
            return hit[1]
    return _analysis(symbol)


def _report_pdf(symbol: str) -> bytes:
    now=time.time()
    with _cache_lock:
        hit=_pdf_cache.get(symbol)
        if hit and now-hit[0] <= PDF_CACHE_SECONDS:
            return hit[1]
        event=_pdf_inflight.get(symbol)
        if event is None:
            event=threading.Event(); _pdf_inflight[symbol]=event; owner=True
        else:
            owner=False
    if not owner:
        event.wait()
        with _cache_lock:
            hit=_pdf_cache.get(symbol)
            if hit:
                return hit[1]
    try:
        started=time.perf_counter()
        data=report_pdf.build_report_pdf(_analysis_for_report(symbol), include_chart=True)
        elapsed=time.perf_counter()-started
        log.info("PDF %s built in %.2fs (%d bytes)", symbol, elapsed, len(data))
        with _cache_lock:
            _pdf_cache[symbol]=(time.time(),data)
        return data
    finally:
        if owner:
            with _cache_lock:
                _pdf_inflight.pop(symbol,None); event.set()

def message_page(title: str, message: str, suggestions: list[str] | None = None, detail: str = "") -> str:
    links = ""
    if suggestions:
        links = "<p>Gợi ý: " + " ".join(
            f'<a href="/analysis/{html.escape(s)}">{html.escape(s)}</a>' for s in suggestions) + "</p>"
    return f"""<!doctype html><html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>
<style>:root{{color-scheme:dark}}body{{margin:0;font-family:system-ui;background:#0b1020;color:#e9eefb;display:grid;place-items:center;min-height:100vh}}main{{max-width:540px;padding:30px}}h1{{font-size:22px}}p{{color:#8f9bb3;line-height:1.55}}a{{color:#5b8cff;margin-right:10px;font-weight:700}}.row{{display:flex;gap:8px;margin-top:20px}}input{{flex:1;background:#11182a;border:1px solid #25314b;color:#fff;border-radius:10px;padding:11px;text-transform:uppercase}}button{{background:#5b8cff;border:0;color:#fff;border-radius:10px;padding:11px 15px;font-weight:700;cursor:pointer}}small{{color:#68738a}}</style>
</head><body><main><h1>{html.escape(title)}</h1><p>{html.escape(message)}</p>{links}
<div class="row"><input id="q" placeholder="Nhập mã, VD: FPT" maxlength="10" autofocus><button id="go">Phân tích</button></div><p><small>{html.escape(detail)}</small></p></main>
<script>const go=()=>{{const s=(document.getElementById('q').value||'').trim().toUpperCase().replace(/[^A-Z0-9]/g,'');if(s)location.href='/analysis/'+s}};document.getElementById('go').onclick=go;document.getElementById('q').addEventListener('keydown',e=>{{if(e.key==='Enter')go()}})</script></body></html>"""


def investment_page(result: dict | None = None) -> str:
    template = INVESTMENT_TEMPLATE.read_text(encoding="utf-8")
    return template.replace("__ANALYSIS_JSON__", _safe_json(result or {}))


def chart_page(symbol: str, period: str) -> str:
    key, now = (symbol, period), time.time()
    with _cache_lock:
        hit = _page_cache.get(key)
        if hit and now - hit[0] <= PAGE_CACHE_SECONDS:
            return hit[1]
    frame, info = charts.build_chart_frame(symbol)
    page = charts.render_dashboard_html(frame, info, period, server_mode=True)
    with _cache_lock:
        _page_cache[key] = (now, page)
        for old, (ts, _) in list(_page_cache.items()):
            if now - ts > PAGE_CACHE_SECONDS:
                _page_cache.pop(old, None)
    return page


def _resolve_symbol_or_error(raw: str):
    symbol = raw.upper().strip()
    if not SYMBOL_RE.match(symbol):
        return None, (400, message_page("Mã không hợp lệ", "Mã chỉ gồm chữ và số, ví dụ FPT, VNM, HPG."))
    resolution = service.resolve_symbol(symbol)
    if resolution.get("status") == "suggest":
        return None, (404, message_page(f"Không tìm thấy mã {symbol}", "Kiểm tra lại mã trên HOSE/HNX.",
                                        suggestions=resolution.get("suggestions") or []))
    if resolution.get("status") == "none":
        return None, (404, message_page(f"Không tìm thấy mã {symbol}", "Hãy nhập một mã cổ phiếu HOSE/HNX."))
    return resolution.get("symbol", symbol), None


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "StockOpportunityAnalyzer/1.0"

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)

    def _headers(self, status: int, content_type: str, length: int, *, filename: str | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()

    def _write_response(self, status: int, data: bytes, content_type: str, filename: str | None = None):
        try:
            self._headers(status, content_type, len(data), filename=filename)
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            # Trình duyệt có thể refresh/đóng tab trong lúc request dài đang chạy.
            # Đây không phải lỗi nghiệp vụ; bỏ qua để tránh traceback giả trong console.
            log.debug("Client disconnected before response was sent")

    def _send(self, status: int, body: str, content_type: str = "text/html; charset=utf-8"):
        data = body.encode("utf-8")
        self._write_response(status, data, content_type)

    def _send_json(self, status: int, payload: dict):
        data = _safe_json(payload).encode("utf-8")
        self._write_response(status, data, "application/json; charset=utf-8")

    def _send_bytes(self, status: int, data: bytes, content_type: str, filename: str | None = None):
        self._write_response(status, data, content_type, filename=filename)

    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
        path = url.path.rstrip("/") or "/"
        if path == "/healthz":
            return self._send(200, "ok", "text/plain; charset=utf-8")
        if path == "/":
            return self._send(200, investment_page())

        match = re.fullmatch(r"/analysis/([^/]+)", path)
        if match:
            symbol, error = _resolve_symbol_or_error(match.group(1))
            if error:
                return self._send(*error)
            try:
                return self._send(200, investment_page(_analysis(symbol)))
            except service.VnstockRateLimited:
                return self._send(503, message_page("Nguồn dữ liệu đang giới hạn truy cập", "Thử tải lại sau khoảng một phút."))
            except Exception as exc:
                log.exception("analysis %s", symbol)
                return self._send(500, message_page(f"Chưa phân tích được {symbol}", "Một hoặc nhiều nguồn dữ liệu đang lỗi.",
                                                    detail=f"{type(exc).__name__}: {str(exc)[:220]}"))

        match = re.fullmatch(r"/api/analysis/([^/]+)", path)
        if match:
            symbol, error = _resolve_symbol_or_error(match.group(1))
            if error:
                return self._send_json(error[0], {"error": "invalid_symbol"})
            try:
                return self._send_json(200, _analysis(symbol))
            except Exception as exc:
                return self._send_json(500, {"error": type(exc).__name__, "message": str(exc)[:220]})

        match = re.fullmatch(r"/api/backtest/([^/]+)", path)
        if match:
            symbol, error = _resolve_symbol_or_error(match.group(1))
            if error:
                return self._send_json(error[0], {"error": "invalid_symbol"})
            query = parse_qs(url.query)
            raw_months = query.get("months", [None])[0]
            raw_years = query.get("years", [None])[0]
            try:
                if raw_months is not None:
                    opportunity_backtest._settings_for_period(months=raw_months)
                    kwargs = {"months": int(float(raw_months))}
                else:
                    raw_years = raw_years if raw_years is not None else "1"
                    opportunity_backtest._settings_for_period(years=raw_years)
                    kwargs = {"years": int(float(raw_years))}
            except ValueError:
                return self._send_json(400, {
                    "status": "FAILED",
                    "error": "invalid_period",
                    "message": opportunity_backtest.PERIOD_ERROR,
                })
            try:
                return self._send_json(200, opportunity_backtest.run_backtest(symbol, **kwargs))
            except service.DataUnavailable as exc:
                return self._send_json(422, {"status": "INSUFFICIENT_DATA", "error": "insufficient_data", "message": str(exc)[:280]})
            except Exception as exc:
                log.exception("backtest %s", symbol)
                return self._send_json(500, {"status": "FAILED", "error": type(exc).__name__, "message": str(exc)[:280]})

        match = re.fullmatch(r"/api/peers/([^/]+)", path)
        if match:
            symbol, error = _resolve_symbol_or_error(match.group(1))
            if error:
                return self._send_json(error[0], {"error": "invalid_symbol"})
            try:
                peers = multi_stock_validation.get_peer_symbols(symbol, limit=5)
                return self._send_json(200, {"symbol": symbol, "symbols": peers})
            except Exception as exc:
                log.exception("peers %s", symbol)
                return self._send_json(500, {"error": type(exc).__name__, "message": str(exc)[:280]})
            
        if path == "/api/validate":
            try:
                raw = parse_qs(url.query).get("symbols", [""])[0]
                symbols = [x.strip().upper() for x in raw.split(",") if x.strip()] or multi_stock_validation.DEFAULT_SYMBOLS
                symbols = [x for x in symbols if SYMBOL_RE.match(x)][:15]
                rows = multi_stock_validation.validate_many(symbols)
                return self._send_json(200, {"symbols": symbols, "rows": rows, "count": len(rows)})
            except Exception as exc:
                log.exception("multi validation")
                return self._send_json(500, {"error": type(exc).__name__, "message": str(exc)[:280]})

        if path == "/api/collect":
            try:
                raw = parse_qs(url.query).get("symbols", [""])[0]
                symbols = [x.strip().upper() for x in raw.split(",") if x.strip()]
                symbols = [x for x in symbols if SYMBOL_RE.match(x)][:10]
                if not symbols:
                    return self._send_json(400, {"error": "symbols_required"})
                data = multi_stock_collector.collect_many(symbols, max_workers=2, include_news=True)
                return self._send_json(200, {"symbols": symbols, "data": data})
            except Exception as exc:
                log.exception("multi collector")
                return self._send_json(500, {"error": type(exc).__name__, "message": str(exc)[:280]})

        match = re.fullmatch(r"/report/([^/]+)\.pdf", path)
        if match:
            symbol, error = _resolve_symbol_or_error(match.group(1))
            if error:
                return self._send(*error)
            try:
                pdf = _report_pdf(symbol)
                return self._send_bytes(200, pdf, "application/pdf", f"{symbol}_investment_analysis.pdf")
            except Exception as exc:
                log.exception("report %s", symbol)
                return self._send(500, message_page("Chưa tạo được PDF", "Dữ liệu hoặc trình dựng báo cáo đang gặp lỗi.",
                                                    detail=f"{type(exc).__name__}: {str(exc)[:220]}"))

        match = re.fullmatch(r"/chart/([^/]+)", path)
        if match:
            symbol, error = _resolve_symbol_or_error(match.group(1))
            if error:
                return self._send(*error)
            period = charts.normalize_period(parse_qs(url.query).get("p", [""])[0])
            try:
                return self._send(200, chart_page(symbol, period))
            except service.VnstockRateLimited:
                return self._send(503, message_page("Nguồn dữ liệu đang giới hạn truy cập", "Thử tải lại sau khoảng một phút."))
            except Exception as exc:
                log.exception("chart %s", symbol)
                return self._send(500, message_page(f"Chưa lấy được biểu đồ {symbol}", "Thử tải lại sau ít phút.",
                                                    detail=f"{type(exc).__name__}: {str(exc)[:220]}"))

        return self._send(404, message_page("Không có trang này", "Trang chính nằm tại / và /analysis/FPT."))

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path != "/api/chat":
            return self._send_json(404, {"error": "not_found"})
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 16_384)
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            symbol = str(payload.get("symbol", "")).upper().strip()
            question = str(payload.get("question", "")).strip()
            resolved, error = _resolve_symbol_or_error(symbol)
            if error:
                return self._send_json(400, {"error": "invalid_symbol"})
            result = _analysis(resolved)
            return self._send_json(200, {"answer": investment_chat.answer(question, result)})
        except Exception as exc:
            log.exception("chat")
            return self._send_json(500, {"error": type(exc).__name__, "message": str(exc)[:220]})


def start_dashboard_server(host: str, port: int) -> ThreadingHTTPServer | None:
    try:
        server = ThreadingHTTPServer((host, port), DashboardHandler)
    except OSError as exc:
        log.warning("Không mở được dashboard ở %s:%s (%s).", host, port, exc)
        return None
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="dashboard-http", daemon=True).start()
    log.info("Investment dashboard: http://%s:%s/", host, port)
    return server
