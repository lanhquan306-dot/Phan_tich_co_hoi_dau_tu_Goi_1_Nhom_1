"""Run the same investment engine across several symbols and return a compact QA table."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterable

import investment_analysis as investment

# Nhóm rổ chỉ số chuẩn thị trường
MARKET_VN30 = ["FPT", "MWG", "HPG", "VCB", "MBB", "TCB", "SSI", "VNM", "VIC", "VHM"]
DEFAULT_SYMBOLS = ["FPT", "MWG", "HPG", "VCB", "MBB", "SSI", "BVH"]


def get_peer_symbols(symbol: str, limit: int = 5) -> list[str]:
    """Tìm danh sách các mã cùng ngành với mã đang phân tích."""
    s = str(symbol or "").upper().strip()
    try:
        from sector_data import SECTOR_MAP
    except ImportError:
        return DEFAULT_SYMBOLS

    target_sector = SECTOR_MAP.get(s)
    if not target_sector:
        return DEFAULT_SYMBOLS

    # Lấy các mã cùng ngành (đưa mã hiện tại lên đầu tiên)
    peers = [sym for sym, sec in SECTOR_MAP.items() if sec == target_sector and sym != s]
    return [s] + peers[:limit]


def _one(symbol: str) -> dict:
    try:
        r = investment.analyze_investment(symbol)
        return {
            "symbol": symbol,
            "company_name": r.get("company_name", ""),
            "industry": r.get("industry") or r.get("fundamental", {}).get("company_type_label", ""),
            "fundamental_score": r.get("score", {}).get("fundamental", {}).get("score"),
            "technical_score": r.get("score", {}).get("technical", {}).get("score"),
            "total_score": r.get("score", {}).get("score"),
            "coverage_pct": r.get("score", {}).get("coverage_pct"),
            "signal": r.get("signal", {}).get("label", ""),
            "risk": r.get("risk_level", ""),
            "latest_price_date": r.get("technical", {}).get("date"),
            "latest_financial_period": r.get("fundamental", {}).get("latest_period"),
            "status": "OK",
        }
    except Exception as exc:
        return {"symbol": symbol, "status": "ERROR", "error": f"{type(exc).__name__}: {str(exc)[:180]}"}


def validate_many(symbols: Iterable[str] | None = None, *, max_workers: int = 3) -> list[dict]:
    clean = list(dict.fromkeys(str(s).upper().strip() for s in (symbols or DEFAULT_SYMBOLS) if str(s).strip()))[:15]
    rows = []
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, 4))) as pool:
        futures = {pool.submit(_one, s): s for s in clean}
        for fut in as_completed(futures):
            rows.append(fut.result())
    order = {s: i for i, s in enumerate(clean)}
    rows.sort(key=lambda r: order.get(r.get("symbol"), 999))
    return rows