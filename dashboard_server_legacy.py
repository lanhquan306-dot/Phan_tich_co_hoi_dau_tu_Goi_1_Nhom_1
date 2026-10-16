"""Web server nhỏ phục vụ dashboard biểu đồ tương tác, chạy trong thread nền cùng process với bot.

Đường dẫn:
    GET /                  trang nhập mã
    GET /chart/<MÃ>?p=6M   biểu đồ nến tương tác (p: 1M, 3M, 6M, 1Y, ALL)
    GET /healthz           trả "ok" (kiểm tra server còn sống)

Dùng http.server của thư viện chuẩn nên không cần cài Flask/Streamlit.
"""
from __future__ import annotations

import html
import logging
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import chart_service as charts
import stock_service as service

log = logging.getLogger("fintech-dashboard")

SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,10}$")
PAGE_CACHE_SECONDS = 60
_page_cache: dict[tuple[str, str], tuple[float, str]] = {}
_page_lock = threading.Lock()


def message_page(title: str, message: str, suggestions: list[str] | None = None, detail: str = "") -> str:
    """Trang đơn giản cho trang chủ và các trường hợp lỗi, kèm ô nhập mã để thử lại."""
    links = ""
    if suggestions:
        links = "<p>Gợi ý: " + " ".join(
            f'<a href="/chart/{html.escape(s)}">{html.escape(s)}</a>' for s in suggestions) + "</p>"
    return f"""<!doctype html><html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
:root{{color-scheme:dark}}
body{{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#131722;color:#d1d4dc;
  display:grid;place-items:center;min-height:100vh}}
main{{max-width:440px;padding:28px}} h1{{font-size:20px;margin:0 0 8px}} p{{color:#9598a1;line-height:1.5}}
a{{color:#5b8cff;margin-right:10px;font-weight:600}} small{{color:#5d606b}}
.row{{display:flex;gap:8px;margin-top:18px}}
input{{flex:1;background:#1e222d;border:1px solid #2a2e39;color:#d1d4dc;border-radius:8px;padding:10px 12px;
  font-size:15px;text-transform:uppercase}}
button{{background:#2962ff;color:#fff;border:0;border-radius:8px;padding:10px 16px;font-weight:600;cursor:pointer}}
</style></head><body><main>
<h1>{html.escape(title)}</h1><p>{html.escape(message)}</p>{links}
<div class="row"><input id="q" placeholder="Nhập mã, VD: FPT" maxlength="10" autofocus>
<button id="go">Xem biểu đồ</button></div>
<p><small>{html.escape(detail)}</small></p>
</main><script>
const go = () => {{
  const s = (document.getElementById('q').value || '').trim().toUpperCase().replace(/[^A-Z0-9]/g, '');
  if (s) location.href = '/chart/' + s;
}};
document.getElementById('go').onclick = go;
document.getElementById('q').addEventListener('keydown', e => {{ if (e.key === 'Enter') go(); }});
</script></body></html>"""


def chart_page(symbol: str, period: str) -> str:
    """HTML biểu đồ, cache 60 giây để nhiều người mở cùng lúc không phải dựng lại."""
    key, now = (symbol, period), time.time()
    with _page_lock:
        hit = _page_cache.get(key)
        if hit and now - hit[0] <= PAGE_CACHE_SECONDS:
            return hit[1]
    frame, info = charts.build_chart_frame(symbol)
    page = charts.render_dashboard_html(frame, info, period, server_mode=True)
    with _page_lock:
        for old in [k for k, (ts, _) in _page_cache.items() if now - ts > PAGE_CACHE_SECONDS]:
            del _page_cache[old]
        _page_cache[key] = (now, page)
    return page


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # ghi qua logging thay vì in thẳng ra stderr
        log.info("%s %s", self.address_string(), fmt % args)

    def _send(self, status: int, body: str, content_type: str = "text/html; charset=utf-8") -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802 (tên bắt buộc của BaseHTTPRequestHandler)
        url = urlparse(self.path)
        path = url.path.rstrip("/") or "/"

        if path == "/healthz":
            return self._send(200, "ok", "text/plain; charset=utf-8")
        if path in ("/", "/chart"):
            return self._send(200, message_page("Biểu đồ cổ phiếu", "Nhập mã cổ phiếu HOSE/HNX để xem biểu đồ nến."))

        match = re.fullmatch(r"/chart/([^/]+)", path)
        if not match:
            return self._send(404, message_page("Không có trang này", "Đường dẫn đúng có dạng /chart/FPT."))
        symbol = match.group(1).upper()
        if not SYMBOL_RE.match(symbol):
            return self._send(400, message_page("Mã không hợp lệ", "Mã chỉ gồm chữ và số, ví dụ FPT, VNM, HPG."))

        # Kiểm tra mã có niêm yết trước để khỏi gọi VNStock vô ích.
        resolution = service.resolve_symbol(symbol)
        if resolution["status"] == "suggest":
            return self._send(404, message_page(f"Không tìm thấy mã {symbol}", "Kiểm tra lại mã trên HOSE/HNX.",
                                                suggestions=resolution["suggestions"]))
        symbol = resolution.get("symbol", symbol)
        period = charts.normalize_period(parse_qs(url.query).get("p", [""])[0])
        try:
            return self._send(200, chart_page(symbol, period))
        except service.VnstockRateLimited:
            return self._send(503, message_page("VNStock đang chạm giới hạn", "Thử tải lại trang sau khoảng 1 phút."))
        except Exception as exc:
            log.exception("dashboard %s", symbol)
            return self._send(500, message_page(f"Chưa lấy được dữ liệu {symbol}", "Thử tải lại sau ít phút.",
                                                detail=f"{type(exc).__name__}: {str(exc)[:200]}"))


def start_dashboard_server(host: str, port: int) -> ThreadingHTTPServer | None:
    """Chạy server ở thread nền. Không mở được cổng thì chỉ ghi cảnh báo, bot vẫn chạy."""
    try:
        server = ThreadingHTTPServer((host, port), DashboardHandler)
    except OSError as exc:
        log.warning("Không mở được dashboard ở %s:%s (%s). Nút biểu đồ sẽ gửi file HTML.", host, port, exc)
        return None
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="dashboard-http", daemon=True).start()
    log.info("Dashboard đang chạy: http://%s:%s/chart/FPT", host, port)
    return server
