"""Run the investment-analysis website without starting the Telegram bot."""
from __future__ import annotations

from http.server import ThreadingHTTPServer

from config import DASHBOARD_HOST, DASHBOARD_PORT
from dashboard_server import DashboardHandler


def main():
    server = ThreadingHTTPServer((DASHBOARD_HOST, DASHBOARD_PORT), DashboardHandler)
    server.daemon_threads = True
    print(f"Stock Opportunity Analyzer: http://{DASHBOARD_HOST}:{DASHBOARD_PORT}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
