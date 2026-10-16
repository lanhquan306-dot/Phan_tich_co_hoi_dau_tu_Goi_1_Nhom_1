"""Independent multi-symbol raw-data collector.

This module is intentionally separate from scoring. It can be used by TV1/TV4,
validation scripts or future batch jobs without going through the web UI.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Iterable

import investment_analysis as investment
import company_info_service
import news_service
import stock_service as service


def collect_symbol(symbol: str, *, include_news: bool = True) -> dict[str, Any]:
    s = symbol.upper().strip()
    out: dict[str, Any] = {"symbol": s, "ok": True, "errors": {}}

    def grab(name, fn):
        try:
            value = fn()
            out[name] = value
        except Exception as exc:
            out[name] = None
            out["errors"][name] = f"{type(exc).__name__}: {str(exc)[:180]}"

    grab("price_history", lambda: service.get_history(s)[0].to_dict("records"))
    grab("financial_tables", lambda: {
        k: (v.to_dict("records") if hasattr(v, "to_dict") else v)
        for k, v in service.get_fundamental_tables(s)[0].items()
        if k in {"income_q", "balance_q", "source"}
    })
    grab("ratios", lambda: investment.get_ratio_table(s)[0].to_dict("records"))
    grab("company_info", lambda: company_info_service.get_company_info(s)[0])
    if include_news:
        grab("news", lambda: news_service.get_company_news(s, 6)[0])
    out["ok"] = not bool(out["errors"])
    return out


def collect_many(symbols: Iterable[str], *, max_workers: int = 3, include_news: bool = True) -> dict[str, dict[str, Any]]:
    clean = list(dict.fromkeys(str(s).upper().strip() for s in symbols if str(s).strip()))
    result: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, 4))) as pool:
        futures = {pool.submit(collect_symbol, s, include_news=include_news): s for s in clean}
        for fut in as_completed(futures):
            s = futures[fut]
            try:
                result[s] = fut.result()
            except Exception as exc:
                result[s] = {"symbol": s, "ok": False, "errors": {"collector": f"{type(exc).__name__}: {exc}"}}
    return result
