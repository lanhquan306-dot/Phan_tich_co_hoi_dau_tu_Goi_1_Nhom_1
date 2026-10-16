"""Chạy: python selftests.py  — không gọi mạng, không cần Telegram token."""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

import stock_service as fe
import presentation as ui
import source_adapter as sa
import strategy as s
import llm
import conversation as conv
import re
from html.parser import HTMLParser
import chart_service as cs
from config import STRATEGY


def eq(got, want, msg):
    if got != want:
        raise AssertionError(f"{msg}: got={got!r}, want={want!r}")


def approx(got, want, tol=1e-9, msg=""):
    if got is None or abs(got - want) > tol:
        raise AssertionError(f"{msg}: got={got!r}, want={want!r}")


def fund(eps, roe, status="ok", de=0.5):
    return {"eps_yoy": eps, "eps_status": status, "roe": roe, "latest_period": "2026-Q2",
            "available": True, "debt_equity": de, "is_financial": False}


# ------------------------------------------------------------------ cổng thị trường
def index_df(n=120, closes_tail=None, vols_tail=None, above=True, no_volume=False):
    """VN-Index giả: nền tăng đều để Close > MA50 (hoặc giảm để < MA50)."""
    d = pd.bdate_range(end="2026-09-22", periods=n)
    base = np.linspace(1000, 1300, n) if above else np.linspace(1300, 1000, n)
    c = base.copy()
    v = np.full(n, 1e9)
    if closes_tail is not None:
        c[-len(closes_tail):] = closes_tail
    if vols_tail is not None:
        v[-len(vols_tail):] = vols_tail
    if no_volume:
        v[:] = 0
    return pd.DataFrame({"time": d, "open": c, "high": c, "low": c, "close": c, "volume": v})


def with_distribution_days(k):
    """Chèn đúng k phiên phân phối (giảm 0.5%, KL tăng) vào 25 phiên cuối, vẫn trên MA50."""
    df = index_df()
    c, v = df["close"].to_numpy().copy(), df["volume"].to_numpy().copy()
    for j in reversed(range(k)):             # chèn từ phiên cũ tới mới để không ghi đè
        i = len(df) - 3 - 2 * j                  # các phiên cách nhau, nằm trong cửa sổ 25
        c[i] = c[i - 1] * 0.995
        v[i] = v[i - 1] * 1.2
        c[i + 1] = c[i] * 1.004                  # phiên sau tăng lại, không tính
    df["close"], df["volume"] = c, v
    return df


def test_market_gate():
    g = s.market_gate(with_distribution_days(3))
    eq((g["state"], g["distribution_days"], g["buy_threshold"], g["weight_factor"]),
       ("UPTREND", 3, 60, 1.0), "3 phiên phân phối -> UPTREND")
    g = s.market_gate(with_distribution_days(4))
    eq((g["state"], g["distribution_days"], g["buy_threshold"], g["weight_factor"]),
       ("PRESSURE", 4, 75, 0.5), "4 phiên phân phối -> PRESSURE")
    eq(len(g["distribution_dates"]), 4, "liệt kê ngày phân phối")

    # Giảm đúng 0.2% + KL tăng là phân phối; giảm 0.19% thì không.
    df = index_df()
    c, v = df["close"].to_numpy().copy(), df["volume"].to_numpy().copy()
    c[-2], v[-2] = c[-3] * (1 - 0.002), v[-3] * 1.1
    c[-1], v[-1] = c[-2] * (1 - 0.0019), v[-2] * 1.1
    df["close"], df["volume"] = c, v
    eq(s.distribution_days(df)["count"], 1, "biên -0.2%")
    # Giảm mạnh nhưng KL thấp hơn -> không phải phân phối
    df = index_df()
    c, v = df["close"].to_numpy().copy(), df["volume"].to_numpy().copy()
    c[-1], v[-1] = c[-2] * 0.98, v[-2] * 0.9
    df["close"], df["volume"] = c, v
    eq(s.distribution_days(df)["count"], 0, "KL giảm không tính")
    # Phân phối ngoài cửa sổ 25 phiên không tính
    df = index_df()
    c, v = df["close"].to_numpy().copy(), df["volume"].to_numpy().copy()
    c[-27], v[-27] = c[-28] * 0.99, v[-28] * 1.5
    df["close"], df["volume"] = c, v
    eq(s.distribution_days(df)["count"], 0, "ngoài cửa sổ 25")

    g = s.market_gate(index_df(no_volume=True))
    eq((g["state"], g["distribution_days"], g["buy_threshold"]), ("PRESSURE", None, 75), "thiếu volume -> PRESSURE")

    df = index_df(above=False)
    eq(s.market_gate(df)["state"], "DOWNTREND", "dưới MA50 2 phiên")
    df = index_df()
    c = df["close"].to_numpy().copy()
    c[-1] = 1100                                   # rơi xuống dưới MA50 đúng 1 phiên
    df["close"] = c
    g = s.market_gate(df)
    eq((g["state"], g["allow_buy"], g["buy_threshold"]), ("WEAK", False, None), "WEAK")
    eq(s.market_gate(index_df(n=40))["state"], "UNKNOWN", "thiếu dữ liệu")


# ------------------------------------------------------------------ chấm điểm
def test_tier1():
    eq(s.score_tier1(fund(9.99, 9.99))["score"], 0, "dưới ngưỡng")
    eq(s.score_tier1(fund(10.0, 10.0))["score"], 13, "EPS 10 -> 8, ROE 10 -> 5")
    eq(s.score_tier1(fund(20.0, 15.0))["score"], 30, "đủ T1")
    eq(s.score_tier1(fund(None, 15.0, "base_nonpositive"))["score"], 10, "cùng kỳ <= 0")


def price_series(ret_pct, n=300, end="2026-09-22"):
    d = pd.bdate_range(end=end, periods=n)
    c = np.full(n, 100.0)
    c[-64:] = np.linspace(100.0, 100.0 * (1 + ret_pct / 100), 64)   # ROC63 = ret_pct
    return pd.DataFrame({"time": d, "open": c, "high": c, "low": c, "close": c, "volume": 1e6})


def test_t21_delta_roc63():
    def pts(a, b):
        return s.score_t21(s.compute_technical(price_series(a), price_series(b)))["points"]
    eq(pts(25, 10), 15, "Delta đúng 15")
    eq(pts(24.99, 10), 8, "Delta 14.99")
    eq(pts(15, 10), 8, "Delta đúng 5")
    eq(pts(14.99, 10), 0, "Delta 4.99")
    t = s.compute_technical(price_series(25), price_series(10).iloc[:-1])
    eq((t["roc_aligned"], t["delta_roc63"] is not None), (False, True), "VNI thiếu nến cuối")
    t = s.compute_technical(price_series(25, end="2026-09-21"), price_series(10))
    eq((t["roc_aligned"], t["roc_index_date"]), (True, "2026-09-21"), "cắt VNI về ngày của mã")
    t2 = s.score_tier2(s.compute_technical(price_series(25), None))
    eq((t2["components"]["T2.1"]["points"], t2["score_max"] - t2["score_min"]), (None, 15), "thiếu VNI = UNKNOWN")


def test_tier2_tier3():
    tech = {"close": 110, "ma50": 100, "ma150": 90, "high52_ratio": 0.85,
            "roc63": 20, "index_roc63": 0, "delta_roc63": 20}
    eq(s.score_tier2(tech)["score"], 30, "đủ T2")
    base = {"breakout": {"found": True, "in_buy_zone": True, "current_above": True, "age": 0,
                         "pivot": 100, "buy_zone": 107}, "vol_ratio_3d": 1.5, "rsi14": 85}
    eq(s.score_tier3(base)["score"], 40, "đủ T3")
    x = dict(base, breakout=dict(base["breakout"], age=3), vol_ratio_3d=1.2, rsi14=50)
    eq(s.score_tier3(x)["score"], 25, "age 3 -> 15, vol 1.2 -> 5, RSI 50 -> 5")


def test_breakout_and_volume():
    n = 260
    times = [datetime(2025, 1, 1) + timedelta(days=i) for i in range(n)]
    close = [100.0] * (n - 1) + [102.0]
    vol = [100.0] * (n - 3) + [150.0] * 3
    tech = s.compute_technical(pd.DataFrame({"time": times, "open": close, "high": close,
                                             "low": close, "close": close, "volume": vol}))
    eq((tech["breakout"]["found"], tech["breakout"]["age"]), (True, 0), "breakout")
    approx(tech["breakout"]["pivot"], 100.0, msg="pivot đóng băng")
    approx(tech["vol_ratio_3d"], 1.5, msg="volume không chồng lấn")


def strong_tech():
    return {"history_count": 260, "adv20": 20e9, "close": 105, "ma20": 103, "ma50": 100, "ma150": 90,
            "ma50_down": False, "below_ma50_2": False, "high52_ratio": 0.90,
            "rsi14": 60, "vol_ratio": 1.6, "vol_ratio_3d": 1.6, "low20": 95,
            "roc63": 25.0, "index_roc63": 5.0, "delta_roc63": 20.0,
            "breakout": {"found": True, "in_buy_zone": True, "current_above": True, "age": 1,
                         "pivot": 100, "buy_zone": 107}}


def gate(state):
    return {"UPTREND": {"state": "UPTREND", "allow_buy": True, "buy_threshold": 60, "weight_factor": 1.0},
            "PRESSURE": {"state": "PRESSURE", "allow_buy": True, "buy_threshold": 75, "weight_factor": 0.5},
            "WEAK": {"state": "WEAK", "allow_buy": False, "buy_threshold": None, "weight_factor": 0.0}}[state]


def test_evaluate_by_market():
    meta = {"exchange": "HOSE"}
    out = s.evaluate("AAA", strong_tech(), fund(25, 18), gate("UPTREND"), meta)
    eq((out["score"], out["signal"]), (100, "BUY"), "UPTREND 100đ")
    approx(out["weight"], 0.10, msg="UPTREND tỷ trọng chuẩn")
    out = s.evaluate("AAA", strong_tech(), fund(25, 18), gate("PRESSURE"), meta)
    eq(out["signal"], "BUY", "PRESSURE 100đ")
    approx(out["weight"], 0.05, msg="PRESSURE tỷ trọng giảm một nửa")

    # Điểm 70: đủ UPTREND (>=60) nhưng chưa đủ PRESSURE (>=75)
    f70 = fund(None, 0, "base_nonpositive")         # T1 = 0 -> tổng 70
    eq(s.evaluate("AAA", strong_tech(), f70, gate("UPTREND"), meta)["signal"], "BUY", "70 >= 60")
    out = s.evaluate("AAA", strong_tech(), f70, gate("PRESSURE"), meta)
    eq((out["score"], out["signal"], out["weight"]), (70, "HOLD", None), "70 < 75")
    eq(s.evaluate("AAA", strong_tech(), fund(25, 18), gate("WEAK"), meta)["signal"], "HOLD", "WEAK")

    t = strong_tech()
    t["breakout"] = {"found": False, "in_buy_zone": False, "current_above": False, "age": None}
    eq(s.evaluate("AAA", t, fund(25, 18), gate("UPTREND"), meta)["signal"], "HOLD", "không breakout")


# ------------------------------------------------------------------ dữ liệu
def test_roe_ttm():
    income = pd.DataFrame({"item": ["Lợi nhuận sau thuế"], "2026-Q2": [110.0], "2026-Q1": [100.0],
                           "2025-Q4": [100.0], "2025-Q3": [90.0], "2025-Q2": [80.0]})
    balance = pd.DataFrame({"item": ["Vốn chủ sở hữu"], "2026-Q2": [1300.0], "2026-Q1": [1280.0],
                            "2025-Q4": [1260.0], "2025-Q3": [1240.0], "2025-Q2": [1200.0]})
    approx(fe._compute_roe_ttm(income, balance), 400 / 1270 * 100, 1e-9, "ROE TTM")


def test_vci_price_board_columns():
    cols = pd.MultiIndex.from_tuples([("listing", "symbol"), ("listing", "ref_price"),
                                      ("match", "match_price"), ("match", "accumulated_volume")])
    row = fe._quote_row(pd.DataFrame([["VIC", 235.1, 243.8, 1.5e6]], columns=cols))
    eq(fe.pick(row, ["match_price"], price=True), 243800, "giá VCI MultiIndex")
    eq(fe.pick(row, ["ref_price"], price=True), 235100, "giá tham chiếu VCI")


def test_source_order():
    calls, orig = [], sa._history_once
    try:
        def fake(symbol, source, start, end, interval="1D"):
            calls.append(source)
            if source != "KBS":
                raise sa.ProviderUnavailable(source + " down")
            return sa.ProviderResult(pd.DataFrame({"time": ["2026-09-22"], "close": [1]}), source, "fake")
        sa._history_once = fake
        r = sa.fetch_history("VNM", sources=["VCI", "MAS", "KBS"], start="2026-01-01", end="2026-09-22")
        eq((calls, r.source), (["VCI", "MAS", "KBS"], "KBS"), "fallback theo thứ tự")
    finally:
        sa._history_once = orig


def test_cards_render():
    """analyze_symbol + card chạy trọn với dữ liệu giả (không mạng)."""
    rng = np.random.default_rng(1)
    d = pd.bdate_range(end="2026-09-21", periods=300)
    c = 50000 * np.exp(np.cumsum(rng.normal(0.002, 0.015, 300)))
    stock = pd.DataFrame({"time": d, "open": c, "high": c, "low": c, "close": c, "volume": 2e6})
    idx = with_distribution_days(4)
    saved = (fe.get_history, fe.get_index_history, fe.listing_map, fe.get_quote, fe.get_fundamental_tables)
    try:
        fe.get_history = lambda sym: (stock, False, False)
        fe.get_index_history = lambda: (idx, False, False)
        fe.listing_map = lambda: {"AAA": {"exchange": "HOSE", "name": "Test"}}
        fe.get_quote = lambda sym: (_ for _ in ()).throw(RuntimeError("offline"))
        fe.get_fundamental_tables = lambda sym: (_ for _ in ()).throw(RuntimeError("offline"))
        r = fe.analyze_symbol("AAA")
        text = ui.signal_card(r)
        assert "Tỷ trọng khuyến nghị" in text and "PRESSURE" in text, text
        m = ui.market_card()
        assert "Phiên phân phối 4/25" in m and "giảm một nửa" in m and "PRESSURE" in m, m
        for name, card in (("signal", text), ("market", m), ("conversational", ui.conversational_stock_card(r, "x")),
                           ("top", ui.top_card([r])), ("quote", ui.quote_card("AAA", r["quote"]))):
            assert_telegram_html(card, name)
    finally:
        fe.get_history, fe.get_index_history, fe.listing_map, fe.get_quote, fe.get_fundamental_tables = saved



def test_llm_context_grounding():
    """LLM context phải lấy số trực tiếp từ engine, không tự gọi mạng."""
    result = {
        "symbol": "AAA", "company_name": "Test", "exchange": "HOSE",
        "confirmed_bar_date": "2026-09-21", "index_bar_date": "2026-09-21",
        "quote": {"price": 101000, "percent_change": 1.5, "volume": 2_000_000,
                  "retrieved_at": "2026-09-22T10:00:00+07:00", "data_source": "VCI"},
        "price_source": "VCI", "market": {"state": "UPTREND", "close": 1800, "ma50": 1750,
        "distribution_days": 2, "allow_buy": True, "buy_threshold": 60, "reason": "test"},
        "fund": {"latest_period": "2026-Q2", "fund_source": "VCI", "eps_yoy": 25,
                 "eps_status": "ok", "roe": 18, "debt_equity": 0.5, "is_financial": False},
        "tech": {"close": 100000, "ma20": 98000, "ma50": 95000, "ma150": 90000,
                 "rsi14": 62, "vol_ratio_3d": 1.6, "high52_ratio": 0.9,
                 "roc63": 20, "index_roc63": 8, "delta_roc63": 12, "breakout": {"found": True}},
        "signal": "BUY", "hold_type": None, "score": 80, "score_min": 80, "score_max": 80,
        "score_complete": True, "threshold": 60, "weight": 0.1, "buy_zone": 105000,
        "breakout_pivot": 98000, "stop_ref": 93000, "reasons": ["Đủ điều kiện"], "checks": {},
        "tier1": {"score": 30, "max": 30, "components": {}},
        "tier2": {"score": 25, "score_min": 25, "score_max": 25, "max": 30, "components": {}},
        "tier3": {"score": 25, "max": 40, "components": {}},
        "warnings": [], "errors": [],
    }
    ctx = llm.build_stock_context(result)
    eq(ctx["decision"]["signal"], "BUY", "LLM signal lấy từ engine")
    eq(ctx["decision"]["score"], 80, "LLM score lấy từ engine")
    eq(ctx["quote"]["price"], 101000.0, "LLM quote lấy từ engine")
    eq(ctx["technical"]["delta_roc63_pct_point"], 12.0, "LLM DeltaROC lấy từ engine")


def test_multi_symbol_resolution():
    saved = fe.listing_map
    try:
        fe.listing_map = lambda: {
            "FPT": {"exchange": "HOSE", "name": "FPT", "all_names": ["FPT"]},
            "VCB": {"exchange": "HOSE", "name": "VCB", "all_names": ["VCB"]},
            "VNM": {"exchange": "HOSE", "name": "VNM", "all_names": ["VNM"]},
        }
        eq(fe.resolve_symbols("fpt với VCB nên mua mã nào?"), ["FPT", "VCB"], "resolve nhiều ticker")
        eq(fe.resolve_symbols("ROE của FPT là gì?"), ["FPT"], "không nhầm ROE thành ticker")
    finally:
        fe.listing_map = saved


def test_conversational_cards():
    def fake(symbol, signal, score):
        return {
            "symbol": symbol, "signal": signal, "hold_type": "CHỜ" if signal == "HOLD" else None,
            "score": score, "score_min": score, "score_max": score, "score_complete": True,
            "threshold": 75, "quote": {"price": 66000, "percent_change": 1.2, "volume": 2_000_000},
            "gtgd": 132e9, "market": {"state": "PRESSURE"},
            "fund": {"eps_yoy": 20, "roe": 15, "latest_period": "2026-Q2"},
            "tech": {"delta_roc63": 10, "rsi14": 60, "vol_ratio_3d": 1.5, "high52_ratio": .9,
                     "breakout": {"found": True, "pivot": 65000, "buy_zone": 69550}},
            "tier1": {"score": 30}, "tier2": {"score_min": 20}, "tier3": {"score": 30},
            "hard": {"pass": True}, "confirmed_bar_date": "2026-09-22", "warnings": [],
        }
    a, b = fake("FPT", "HOLD", 70), fake("VCB", "BUY", 80)
    one = ui.conversational_stock_card(a, "FPT đang HOLD theo engine.")
    comp = ui.comparison_card([a, b], "VCB phù hợp hơn với strategy ở snapshot này.")
    assert "Chi tiết chấm điểm" in one and "FPT" in one and "HOLD" in one
    assert "SO SÁNH FPT · VCB" in comp and "<pre>" in comp and "PASS" in comp
    assert "Phù hợp chiến lược nhất: <b>VCB</b>" in comp
    assert_telegram_html(one, "conversational")
    assert_telegram_html(comp, "comparison")
    degraded = ui.comparison_card([a, b], conv.comparison_summary({"FPT": a, "VCB": b}))
    assert "VCB" in degraded and "Tín hiệu" in degraded


def test_conversation_router_and_fallback():
    prev = {"OCB": {"symbol": "OCB"}}
    eq(conv.classify_intent("giá OCB bao nhiêu", ["OCB"], prev), "quote", "quote intent")
    eq(conv.classify_intent("OCB với VIC nên chọn mã nào", ["OCB", "VIC"], prev), "compare", "compare intent")
    eq(conv.classify_intent("tại sao chỉ 30 điểm?", [], prev), "explain", "follow-up explain intent")
    eq(conv.contextual_symbols("so với VIC thì sao?", ["VIC"], prev), ["OCB", "VIC"], "inherit previous ticker for comparison")
    eq(conv.contextual_symbols("tại sao?", [], prev), ["OCB"], "pronoun/follow-up ticker memory")

    r = {
        "symbol": "OCB", "signal": "HOLD", "score": 30, "score_min": 30, "score_max": 30,
        "score_complete": True, "threshold": 75, "hold_type": "CHỜ",
        "market": {"state": "PRESSURE", "allow_buy": True},
        "checks": {"mandatory_breakout": False},
        "hard": {"pass": True, "checks": {}},
        "fund": {"eps_yoy": 22.7, "roe": 13.0},
        "tech": {"close": 10450, "ma50": 10600, "ma50_down": True, "delta_roc63": 0.3,
                 "rsi14": 47.2, "vol_ratio_3d": 0.63, "breakout": {"found": False}},
        "tier1": {"score": 25, "components": {}},
        "tier2": {"score": 5, "score_min": 5, "score_max": 5, "score_complete": True, "components": {}},
        "tier3": {"score": 0, "components": {}},
        "reasons": ["T3.1 = 0: chưa breakout"],
    }
    text = conv.single_analysis_summary(r)
    assert "HOLD 30/100" in text and "T1 25/30" in text and "breakout" in text.lower() and "MA50" in text
    why = conv.explanation_summary(r, "tại sao chỉ 30 điểm?")
    assert "OCB" in why and "T3 0/40" in why
    assert not conv.ai_response_is_useful("FINTECH COPILOT gửi báo cáo phân tích mã", "analysis", ["OCB"]), "reject generic AI prose"



def test_agent_router_offline():
    """Core chatbot intents must still work when Groq is unavailable."""
    def fake(symbol, score=30):
        return {
            "symbol": symbol, "company_name": symbol, "exchange": "HOSE",
            "signal": "HOLD", "hold_type": "CHỜ", "score": score, "score_min": score, "score_max": score,
            "score_complete": True, "threshold": 75, "weight": None, "buy_zone": None,
            "breakout_pivot": None, "stop_ref": 9700,
            "quote": {"price": 10450, "percent_change": 0.0, "volume": 100000, "open": 10400, "high": 10500, "low": 10300},
            "price_source": "KBS", "gtgd": 5.8e9, "confirmed_bar_date": "2026-09-22", "index_bar_date": "2026-09-22",
            "market": {"state": "PRESSURE", "allow_buy": True, "buy_threshold": 75, "close": 1800, "ma50": 1775,
                       "distribution_days": 4, "reason": "test"},
            "fund": {"eps_yoy": 22.7, "roe": 13.0, "latest_period": "2026-Q2", "fund_source": "VCI",
                     "debt_equity": 1.0, "is_financial": True},
            "tech": {"close": 10450, "ma20": 10500, "ma50": 10600, "ma150": 10000, "ma50_down": True,
                     "delta_roc63": 0.3, "roc63": 1.0, "index_roc63": 0.7, "rsi14": 47.2,
                     "vol_ratio_3d": 0.63, "high52_ratio": 0.7, "breakout": {"found": False}},
            "tier1": {"score": 25, "max": 30, "components": {}},
            "tier2": {"score": 5, "score_min": 5, "score_max": 5, "score_complete": True, "max": 30, "components": {}},
            "tier3": {"score": 0, "max": 40, "components": {}},
            "hard": {"pass": True, "checks": {}}, "checks": {"mandatory_breakout": False},
            "reasons": ["T3.1 = 0: chưa breakout"], "warnings": [], "errors": [],
        }

    saved = (fe.resolve_symbols, fe.analyze_symbol, fe.listing_map, fe.get_market_gate, fe.get_quote, llm.get_client)
    try:
        fe.resolve_symbols = lambda q, max_symbols=5: (["OCB", "VIC"] if "OCB" in q.upper() and "VIC" in q.upper()
                                                     else ["OCB"] if "OCB" in q.upper()
                                                     else ["VIC"] if "VIC" in q.upper() else [])
        fe.analyze_symbol = lambda symbol, position=None: fake(symbol, 38 if symbol == "VIC" else 30)
        fe.listing_map = lambda: {"OCB": {}, "VIC": {}}
        fe.get_market_gate = lambda: (None, {"state": "PRESSURE", "allow_buy": True, "buy_threshold": 75,
                                               "close": 1800, "ma50": 1775, "distribution_days": 4, "reason": "test"}, False)
        fe.get_quote = lambda symbol: ({"price": 10450, "percent_change": 0, "open": 10400, "high": 10500,
                                        "low": 10300, "volume": 100000, "data_source": "KBS", "retrieved_at": "now"}, False, False)
        llm.get_client = lambda: None

        one = llm.run_financial_agent("cổ phiếu OCB")
        eq(one.mode, "analysis", "offline analysis route")
        assert "HOLD 30/100" in one.text

        prev = {"OCB": fake("OCB")}
        why = llm.run_financial_agent("tại sao?", previous_results=prev, recent_symbols=["OCB"])
        eq(why.mode, "explain", "offline explain route")
        assert "reuse_analysis(OCB)" in why.tool_trace

        quote = llm.run_financial_agent("giá nó bao nhiêu?", previous_results=prev, recent_symbols=["OCB"])
        eq(quote.mode, "quote", "contextual quote route")
        assert "10,450" in quote.text

        comp = llm.run_financial_agent("so với VIC thì sao?", previous_results=prev, recent_symbols=["OCB"])
        eq(comp.mode, "comparison", "contextual compare route")
        eq(list(comp.analyses.keys()), ["OCB", "VIC"], "comparison inherits previous ticker")
    finally:
        fe.resolve_symbols, fe.analyze_symbol, fe.listing_map, fe.get_market_gate, fe.get_quote, llm.get_client = saved

def test_chart_service():
    """Biểu đồ: chỉ báo đúng công thức, PNG/HTML render được, nhận diện câu chat."""
    rng = np.random.default_rng(1)
    n = 300
    d = pd.bdate_range(end="2026-09-22", periods=n)
    c = 50000 * np.exp(np.cumsum(rng.normal(0, 0.015, n)))
    o = np.r_[c[0], c[:-1]]
    df = pd.DataFrame({"time": d, "open": o, "high": np.maximum(o, c) * 1.01,
                       "low": np.minimum(o, c) * 0.99, "close": c, "volume": rng.integers(1e5, 1e7, n)})
    f = cs.compute_indicators(df)
    approx(f["ma20"].iloc[-1], float(np.mean(c[-20:])), 1e-6, "MA20")
    approx(f["ma150"].iloc[-1], float(np.mean(c[-150:])), 1e-6, "MA150")
    assert pd.isna(f["ma150"].iloc[148]) and not pd.isna(f["ma150"].iloc[149])
    approx(f["rsi14"].iloc[-1], float(s.wilder_rsi(pd.Series(c)).iloc[-1]), 1e-9, "RSI14 = strategy.wilder_rsi")
    info = {"symbol": "FPT", "source": "VCI", "exchange": "HOSE", "name": "FPT"}
    png = cs.render_chart_png(f, info, "6M")
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 20_000
    cap = cs.chart_caption(f, info, "6M")
    assert len(cap) <= 1024 and "MA150" in cap and "RSI14" in cap
    assert "BUY" not in cap and "SELL" not in cap
    page = cs.render_dashboard_html(f, info, "3M")
    assert "LightweightCharts" in page and '"symbol":"FPT"' in page and "__DATA_JSON__" not in page
    eq(cs.normalize_period("1N"), "1Y", "period alias")
    eq(cs.parse_period_token("FPT"), None, "ticker is not a period")
    assert cs.is_chart_request("biểu đồ FPT") and cs.is_chart_request("chart hpg")
    assert not cs.is_chart_request("FPT có nên mua không?")
    eq(cs.period_from_text("đồ thị VNM 1 năm"), "1Y", "period from text")



# ------------------------------------------------------------------ giao diện Telegram
class _TelegramHTML(HTMLParser):
    """Kiểm tra đúng quy tắc HTML của Telegram Bot API."""
    ALLOWED = {"b", "i", "u", "s", "code", "pre", "a", "blockquote", "tg-spoiler"}
    BLOCKS = {"blockquote", "code", "pre"}

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.stack, self.errors = [], []

    def handle_starttag(self, tag, attrs):
        if tag not in self.ALLOWED:
            self.errors.append(f"thẻ không hỗ trợ <{tag}>")
        if tag in self.BLOCKS and any(t in self.BLOCKS for t in self.stack):
            self.errors.append(f"<{tag}> lồng trong {self.stack}")
        if any(t in ("code", "pre") for t in self.stack):
            self.errors.append(f"<{tag}> nằm trong code/pre")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"đóng sai </{tag}>, đang mở {self.stack}")
        else:
            self.stack.pop()


def assert_telegram_html(text: str, name: str):
    parser = _TelegramHTML()
    parser.feed(text)
    errors = parser.errors + ([f"chưa đóng {parser.stack}"] if parser.stack else [])
    if re.search(r"&(?!(lt|gt|amp|quot);)", text):
        errors.append("ký tự & chưa escape")
    if len(text) > 4096:
        errors.append(f"dài {len(text)} > 4096")
    if errors:
        raise AssertionError(f"Thẻ {name} sai định dạng Telegram: {errors}")


def test_ui_cards():
    """Giao diện: thanh tiến độ, bản đồ mốc giá, thẻ vị thế, thẻ giá và escape dữ liệu lạ."""
    eq(ui.progress_bar(58, 100), "▰▰▰▰▰▰▱▱▱▱", "thanh điểm")
    eq(ui.range_bar(100, 110, 105, 11), "━━━━━●━━━━━", "vị trí giá trong biên độ")
    r = {"quote": {"price": 100}, "stop_ref": 93, "tech": {"close": 100, "ma20": 98, "ma50": 95, "high52": 120,
         "low20": 90, "breakout": {"found": True, "pivot": 97, "buy_zone": 103.79}}}
    levels = ui.price_map(r)
    eq(levels[0].split(" ")[1], "Đỉnh", "mốc cao nhất đứng đầu")
    assert any("Giá hiện tại" in x for x in levels) and levels[-1].startswith("🛟"), levels
    card = ui.positions_card({"FPT": {"entry_price": 100, "peak_close": 120, "entry_date": "2026-09-01"}}, {"FPT": 110})
    assert "+10.00%" in card and "Trailing stop" in card, card
    quote = ui.quote_card("A&B", {"price": 10, "reference": 9, "percent_change": 11.1, "low": 9, "high": 11})
    assert "A&amp;B" in quote and "🔺" in quote, quote
    for name, text in (("positions", card), ("quote", quote), ("agent", ui.agent_text_card("<script> & co"))):
        assert_telegram_html(text, name)


# ------------------------------------------------------------------ backtest
def _bt_frame(days, closes, opens=None):
    c = np.asarray(closes, float)
    o = c.copy() if opens is None else np.asarray(opens, float)
    return pd.DataFrame({"time": days, "open": o, "high": np.maximum(o, c), "low": np.minimum(o, c),
                         "close": c, "volume": np.full(len(days), 1e6)})


def test_backtest_helpers():
    """Chu kỳ BCTC theo ngày công bố, cắt quý, MDD, trần/sàn, khoảng thời gian."""
    from datetime import date
    import backtest as bt
    eq(bt.available_quarter(date(2026, 5, 14), 45), (2025, 4), "Q1 chưa công bố trước 15/05")
    eq(bt.available_quarter(date(2026, 5, 15), 45), (2026, 1), "Q1 dùng được từ 15/05")
    eq(bt.available_quarter(date(2026, 2, 14), 45), (2025, 4), "Q4 dùng được từ 14/02")
    df = pd.DataFrame({"item": ["x"], "2025-Q4": [1], "2026-Q1": [2], "2026-Q2": [3]})
    eq(list(fe.truncate_quarters(df, (2026, 1)).columns), ["item", "2025-Q4", "2026-Q1"], "cắt quý tương lai")
    dd = bt.max_drawdown(pd.Series([100, 120, 90, 130, 117]))
    approx(dd["mdd"], -25.0, 1e-9, "MDD 120 -> 90")
    assert bt.at_ceiling(21400, 20000, "HOSE") and not bt.at_ceiling(21300, 20000, "HOSE")
    assert bt.at_ceiling(10950, 10000, "HNX") and bt.locked_floor(18600, 20000, "HOSE")
    s0, e0 = bt.resolve_period(months=6, today=date(2026, 9, 24))
    eq((s0, e0), (date(2026, 3, 24), date(2026, 9, 24)), "6 tháng gần nhất")
    eq(bt.resolve_period("01/01/2026", "2026-06-30")[0], date(2026, 1, 1), "ngày dd/mm/yyyy")


def test_backtest_engine_rules():
    """Tín hiệu t -> khớp Open t+1, lô 100, phí + thuế, T+2, bỏ qua giá trần, giới hạn vị thế."""
    from dataclasses import replace
    from datetime import date
    import backtest as bt
    from config import BACKTEST

    days = pd.bdate_range("2025-01-01", "2026-06-30")
    n = len(days)
    a, b, c = _bt_frame(days, np.full(n, 20000.0)), _bt_frame(days, np.full(n, 50000.0)), \
        _bt_frame(days, np.full(n, 30000.0))
    idx = _bt_frame(days, np.linspace(1000, 1300, n))
    start, end = date(2026, 1, 5), date(2026, 3, 31)
    cal = [t for t in days if pd.Timestamp(start) <= t <= pd.Timestamp(end)]
    a.loc[a.time == cal[3], "open"] = 20500                   # Open t+1 trong vùng mua
    b.loc[b.time == cal[3], ["open", "high", "close"]] = 53500  # Open t+1 = giá trần (+7%)
    script = {("AAA", cal[2]): "BUY", ("BBB", cal[2]): "BUY", ("CCC", cal[2]): "BUY", ("AAA", cal[3]): "SELL"}
    seen = []

    def scripted(sym, tech, fund, gate, meta, position=None, cfg=None):
        day = pd.Timestamp(tech["roc_stock_date"])
        seen.append(day)
        sig = script.get((sym, day), "HOLD")
        return {"signal": "HOLD" if sig == "SELL" and not position else sig, "score": 80, "threshold": 60,
                "reasons": ["test"], "breakout_pivot": {"AAA": 20000, "BBB": 50000, "CCC": 29000}[sym],
                "buy_zone": {"AAA": 21400, "BBB": 53500, "CCC": 31000}[sym]}

    saved = s.evaluate, bt.FundamentalAsOf.get
    s.evaluate, bt.FundamentalAsOf.get = scripted, (lambda self, sym, day: {})
    try:
        data = bt.BacktestData(prices={"AAA": a, "BBB": b, "CCC": c}, index=idx, fund_tables={},
                               meta={k: {"exchange": "HOSE"} for k in ("AAA", "BBB", "CCC")})
        r = bt.run_backtest(data, start, end, replace(BACKTEST, max_positions=1))
    finally:
        s.evaluate, bt.FundamentalAsOf.get = saved
    eq(max(seen), cal[-1], "phiên cuối vẫn được chấm điểm để đối chiếu với /signal")
    assert all(o["signal_date"] < cal[-1] for o in r["orders"] if o["signal"] == "BUY"), \
        "tín hiệu phiên cuối không được đặt lệnh (không còn phiên t+1)"
    filled = [o for o in r["orders"] if o["status"] == "KHỚP"]
    buy, sell = filled[0], filled[1]
    eq((buy["symbol"], buy["exec_date"], buy["exec_price"]), ("AAA", cal[3], 20500.0), "mua tại Open t+1")
    qty = int(100_000_000 / (20500 * 1.0015) // 100) * 100     # MAX_POSITIONS=1 -> cả 100 triệu, làm tròn lô
    eq(buy["qty"], qty, "khối lượng mua theo vốn phân bổ, làm tròn lô 100")
    eq(sell["exec_date"], cal[5], "tín hiệu bán ngày mua -> chờ T+2 mới bán")
    approx(sell["fee"], qty * 20000 * 0.0015, 1e-6, "phí bán")
    approx(sell["tax"], qty * 20000 * 0.001, 1e-6, "thuế bán 0,1%")
    approx(sell["pnl"], qty * 20000 * (1 - 0.0025) - qty * 20500 * 1.0015, 1e-6, "lãi/lỗ ròng")
    skipped = {o["symbol"]: o["reason"] for o in r["orders"] if o["status"] == "BỎ QUA"}
    assert "TRẦN" in skipped["BBB"] and "vị thế" in skipped["CCC"], skipped
    eq(r["metrics"]["n_trades"], 1, "một giao dịch khứ hồi")
    approx(r["metrics"]["final_equity"], 100_000_000 + sell["pnl"], 1e-6, "vốn cuối = vốn đầu + lãi/lỗ")


def test_backtest_no_lookahead():
    """Đổi dữ liệu SAU một ngày không được làm thay đổi bất kỳ lệnh/tín hiệu nào TRƯỚC ngày đó."""
    from datetime import date
    import backtest as bt
    import presentation as pui

    rng = np.random.default_rng(11)
    days = pd.bdate_range("2024-06-03", "2026-06-30")
    n = len(days)

    def walk(p0, drift, vol):
        r = rng.normal(drift, vol, n)
        c = p0 * np.exp(np.cumsum(r))
        o = np.r_[c[0], c[:-1]] * (1 + rng.normal(0, 0.003, n))
        v = rng.integers(2e6, 6e6, n).astype(float)
        v[r > 0.02] *= 2.5
        return pd.DataFrame({"time": days, "open": o, "high": np.maximum(o, c) * 1.004,
                             "low": np.minimum(o, c) * 0.996, "close": c, "volume": v})

    prices = {f"S{i}": walk(30000, 0.0015, 0.018) for i in range(3)}
    idx = walk(1100, 0.0008, 0.006)
    strong = {"eps_yoy": 30, "eps_status": "ok", "roe": 20, "latest_period": "2026-Q1", "available": True,
              "debt_equity": 0.5, "is_financial": False}
    saved = bt.FundamentalAsOf.get
    bt.FundamentalAsOf.get = lambda self, sym, day: strong
    try:
        meta = {k: {"exchange": "HOSE"} for k in prices}
        start, end, cut = date(2026, 1, 5), date(2026, 5, 29), pd.Timestamp("2026-03-31")
        base = bt.run_backtest(bt.BacktestData(prices, idx, {}, meta), start, end)
        noisy = {k: df.copy() for k, df in prices.items()}
        for df in noisy.values():
            future = df["time"] > cut
            df.loc[future, ["open", "high", "low", "close"]] *= 0.5   # "tương lai" sập 50%
        other = bt.run_backtest(bt.BacktestData(noisy, idx, {}, meta), start, end)
    finally:
        bt.FundamentalAsOf.get = saved

    def before(rep):
        return [(o["status"], o["symbol"], o["side"], o["exec_date"], o["qty"], round(o["exec_price"] or 0, 4))
                for o in rep["orders"] if o["exec_date"] is not None and o["exec_date"] <= cut]
    assert base["metrics"]["n_orders"] > 0, "dữ liệu mẫu phải sinh được lệnh để kiểm tra có ý nghĩa"
    eq(before(other), before(base), "lệnh trước ngày cắt không phụ thuộc dữ liệu tương lai")
    e1 = base["equity"].set_index("date").loc[:cut, "equity"]
    e2 = other["equity"].set_index("date").loc[:cut, "equity"]
    assert np.allclose(e1.to_numpy(), e2.to_numpy()), "equity trước ngày cắt phải giống hệt"
    card = pui.backtest_card(base)
    assert "Win rate" in card and "Sụt giảm tối đa" in card and "Profit Factor" in card and "CHẨN ĐOÁN" in card
    for name, text in (("backtest", card), ("backtest_menu", pui.backtest_menu_text()),
                       ("backtest_progress", pui.backtest_progress_text("A&B", "sim", 5, 10, "x"))):
        assert_telegram_html(text, name)
    png = bt.render_report_png(base)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    csv_text = bt.trade_log_csv(base).decode("utf-8-sig")
    assert csv_text.startswith("trang_thai,ma,chieu,ngay_tin_hieu") and "gia_thuc_hien" in csv_text


def test_backtest_matches_live_signal_and_explains():
    """Phiên cuối của backtest = đúng kết quả engine live cùng ngày; chẩn đoán chỉ ra nút thắt (VD nợ vay)."""
    from datetime import date
    import backtest as bt
    import presentation as pui

    rng = np.random.default_rng(21)
    days = pd.bdate_range("2024-06-03", "2026-09-24")
    n = len(days)
    r_ = rng.normal(0.001, 0.018, n)
    c = 90000 * np.exp(np.cumsum(r_))
    o = np.r_[c[0], c[:-1]]
    px = pd.DataFrame({"time": days, "open": o, "high": np.maximum(o, c) * 1.004, "low": np.minimum(o, c) * 0.996,
                       "close": c, "volume": rng.integers(2e6, 6e6, n).astype(float)})
    ic = 1100 * np.exp(np.cumsum(rng.normal(0.0006, 0.007, n)))
    idx = pd.DataFrame({"time": days, "open": ic, "high": ic, "low": ic, "close": ic,
                        "volume": rng.integers(5e8, 9e8, n).astype(float)})
    heavy_debt = {"eps_yoy": 25, "eps_status": "ok", "roe": 18, "latest_period": "2026-Q2", "available": True,
                  "debt_equity": 1.86, "is_financial": False}
    saved = bt.FundamentalAsOf.get
    bt.FundamentalAsOf.get = lambda self, sym, day: heavy_debt
    try:
        rep = bt.run_backtest(bt.BacktestData({"VIC": px}, idx, {}, {"VIC": {"exchange": "HOSE"}}),
                              date(2026, 3, 24), date(2026, 9, 24))
    finally:
        bt.FundamentalAsOf.get = saved
    # Engine live: 620 ngày lịch kết thúc ở phiên cuối, giống stock_service._load_history.
    end = pd.Timestamp("2026-09-24")
    win = px[px["time"] >= end - pd.Timedelta(days=620)]
    iwin = idx[idx["time"] >= end - pd.Timedelta(days=620)]
    gate = s.market_gate(iwin)
    live = s.evaluate("VIC", s.compute_technical(win, iwin), heavy_debt, gate, {"exchange": "HOSE"})
    info = rep["diagnostics"]["per_symbol"]["VIC"]
    eq((info["last"]["signal"], info["last"]["score"]), (live["signal"], live["score_min"]),
       "phiên cuối backtest khớp /signal")
    eq(rep["metrics"]["n_orders"], 0, "nợ vay/VCSH 1.86 > 1.5 -> không bao giờ BUY")
    eq(info["fail"]["hard_filter"], info["free"], "trượt bộ lọc mọi phiên")
    assert info["hard_reasons"][0][0].startswith("Nợ vay/VCSH > 1.5"), info["hard_reasons"]
    card = pui.backtest_card(rep)
    assert "KHÔNG mở lệnh" in card and "Nợ vay/VCSH" in card and "VÌ SAO" in card, card
    assert_telegram_html(card, "backtest_no_trade")
    log_csv = bt.signal_log_csv(rep).decode("utf-8-sig").splitlines()
    eq(len(log_csv) - 1, rep["sessions"], "nhật ký: mỗi phiên một dòng")


def _regime_market(seed: int, n_sym: int, trending: bool):
    """Dữ liệu giả: trending=True -> giá có "pha" tăng/giảm kéo dài (breakout có đà), False -> bước ngẫu nhiên."""
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2024-01-01", "2026-06-30")
    n = len(days)

    def one(p0):
        if trending:
            drift = np.repeat(rng.choice([0.006, -0.004, 0.0], size=n // 50 + 1), 50)[:n]
            r = drift + rng.normal(0, 0.012, n)
        else:
            r = rng.normal(0.0005, 0.018, n)
        c = p0 * np.exp(np.cumsum(r))
        o = np.r_[c[0], c[:-1]] * (1 + rng.normal(0, 0.002, n))
        v = rng.integers(2e6, 8e6, n).astype(float)
        v[r > 0.015] *= 2.0
        return pd.DataFrame({"time": days, "open": o, "high": np.maximum(o, c) * 1.003,
                             "low": np.minimum(o, c) * 0.997, "close": c, "volume": v})
    prices = {f"T{i}": one(30000) for i in range(n_sym)}
    ic = 1100 * np.exp(np.cumsum(rng.normal(0.0005, 0.006, n)))
    idx = pd.DataFrame({"time": days, "open": ic, "high": ic, "low": ic, "close": ic,
                        "volume": rng.integers(5e8, 9e8, n).astype(float)})
    return prices, idx


def test_signal_validation():
    """Kiểm định tín hiệu: chấm đúng/sai, lợi nhuận tương lai đúng phiên, không look-ahead,
    và bộ kiểm định PHÂN BIỆT được chiến lược có tác dụng (dữ liệu có xu hướng) với ngẫu nhiên."""
    from datetime import date
    import backtest as bt
    import presentation as pui
    import signal_validation as sv

    # Quy tắc chấm của người dùng: BUY đúng khi tăng, SELL khi giảm tiếp, NẮM GIỮ khi không giảm,
    # TRÁNH MUA khi không tăng, CHỜ chỉ đo bỏ lỡ (> 10%).
    eq(sv.actual_move(2.0, 0), "TĂNG", "nhãn thật tăng")
    eq(sv.actual_move(-0.5, 1.0), "ĐI NGANG", "trong biên đi ngang")
    eq(sv.judge_case("BUY", "TĂNG", 2.0, 10), "ĐÚNG", "BUY + tăng")
    eq(sv.judge_case("BUY", "ĐI NGANG", 0.0, 10), "SAI", "BUY + đi ngang = sai")
    eq(sv.judge_case("SELL", "GIẢM", -3.0, 10), "ĐÚNG", "SELL + giảm tiếp")
    eq(sv.judge_case("NẮM GIỮ", "ĐI NGANG", 0.0, 10), "ĐÚNG", "NẮM GIỮ + không giảm")
    eq(sv.judge_case("TRÁNH MUA", "TĂNG", 4.0, 10), "SAI", "TRÁNH MUA + tăng = sai")
    eq(sv.judge_case("CHỜ", "TĂNG", 12.0, 10), "BỎ LỠ", "CHỜ + tăng > 10% = bỏ lỡ")
    eq(sv.judge_case("CHỜ", "TĂNG", 8.0, 10), "KHÔNG BỎ LỠ", "CHỜ + tăng < 10%")
    eq(sv.judge_case("BUY", None, None, 10), "CHƯA CHẤM", "chưa có tương lai")
    eq(sv._correct("BUY", 3.0), 1.0, "BUY đúng khi giá tăng")
    eq(sv._correct("SELL", 3.0), 0.0, "SELL sai khi giá tăng tiếp")
    eq(sv._correct("TRÁNH MUA", -1.0), 1.0, "TRÁNH MUA đúng khi giá giảm")
    assert math.isnan(sv._correct("CHỜ", 5.0)), "CHỜ không chấm đúng/sai"
    approx(sv.spearman(range(20), [x * x for x in range(20)]), 1.0, 1e-12, "Spearman đơn điệu = 1")

    strong = {"eps_yoy": 30, "eps_status": "ok", "roe": 20, "latest_period": "2026-Q1", "available": True,
              "debt_equity": 0.5, "is_financial": False}
    saved = bt.FundamentalAsOf.get
    bt.FundamentalAsOf.get = lambda self, sym, day: strong
    try:
        start, end = date(2025, 3, 3), date(2026, 5, 29)
        prices, idx = _regime_market(3, 8, trending=True)
        data = bt.BacktestData(prices, idx, {}, {k: {"exchange": "HOSE"} for k in prices})
        sig, trades, _ = sv.replay_signals(data, start, end)
        rep = sv.build_report(data, sig, trades, start, end, [], 0)
        # Không look-ahead: làm sập giá sau ngày cắt -> tín hiệu trước ngày cắt giữ nguyên.
        cut = pd.Timestamp("2025-12-31")
        noisy = {k: prices[k].copy() for k in ("T0", "T1", "T2")}
        for df in noisy.values():
            df.loc[df["time"] > cut, ["open", "high", "low", "close"]] *= 0.5
        sig2, _, _ = sv.replay_signals(bt.BacktestData(noisy, idx, {}, {k: data.meta[k] for k in noisy}),
                                       start, date(2026, 1, 30))
    finally:
        bt.FundamentalAsOf.get = saved
    cols = ["date", "symbol", "label", "score"]
    part = sig[sig["symbol"].isin(list(noisy))]
    eq(sig2[sig2["date"] <= cut][cols].values.tolist(), part[part["date"] <= cut][cols].values.tolist(),
       "tín hiệu quá khứ không phụ thuộc giá tương lai")
    # Lợi nhuận 20 phiên sau tính đúng từ close t -> close t+20 của chính mã đó.
    r0 = rep["signals"].iloc[100]
    f = prices[r0["symbol"]].reset_index(drop=True)
    i = int(f.index[f["time"] == r0["date"]][0])
    approx(r0["fwd20"], (f["close"].iloc[i + 20] / f["close"].iloc[i] - 1) * 100, 1e-9, "fwd20")
    st = rep["stats"]
    assert set(st["by_label"]) >= {"BUY", "NẮM GIỮ", "TRÁNH MUA"}, st["by_label"].keys()
    assert st["ic"]["score"] is not None and st["ic"]["score"] > 0.05, st["ic"]
    assert st["by_label"]["BUY"]["mean20"] > st["base"]["mean20"], "dữ liệu có xu hướng: BUY phải tốt hơn mốc"
    icons = dict((text.split(":")[0], icon) for icon, text in rep["verdicts"])
    eq(icons.get("Điểm 0–100"), "✅", "phát hiện điểm số có dự báo trên dữ liệu có xu hướng")
    # Độ chính xác: số đúng khớp đếm tay, tỷ lệ tốt hơn đoán bừa rõ ràng trên dữ liệu có xu hướng.
    acc = rep["accuracy"]
    main = rep["cases"][rep["cases"]["result"].isin(["ĐÚNG", "SAI"])]
    eq(acc["overall"]["correct"], int((main["result"] == "ĐÚNG").sum()), "đếm case đúng")
    manual = [sv.judge_case(r["label"], sv.actual_move(r["fwd20"], 0), r["fwd20"], sv.MISSED_GAIN)
              for r in rep["cases"].to_dict("records")]
    eq(manual, rep["cases"]["result"].tolist(), "mỗi case chấm đúng quy tắc")
    eq(acc["overall"]["icon"], "✅", "dữ liệu có xu hướng: bot đúng hơn đoán bừa rõ ràng")
    eq((rep["cases"]["fwd20"].isna() == (rep["cases"]["result"] == "CHƯA CHẤM")).all(), True,
       "chỉ case chưa có đủ 20 phiên tương lai mới là CHƯA CHẤM")
    r10 = sv.rescore(rep, 10)
    eq(r10["accuracy"]["h"], 10, "chấm lại 10 phiên")
    assert r10["accuracy"]["n_unscored"] <= acc["n_unscored"] and r10["accuracy"]["n_scored"] >= acc["n_scored"]
    card = pui.validation_card(rep, [{"symbol": "T0", "ok": True, "detail": "replay 40/100 · live 40/100"}])
    assert_telegram_html(card, "validation")
    assert "BOT ĐÚNG" in card and "Đoán bừa" in card and "HÔM NAY" in card and "KẾT LUẬN" in card, card
    assert sv.render_accuracy_png(rep, "T0")[:8] == b"\x89PNG\r\n\x1a\n"
    assert sv.render_accuracy_summary_png(rep)[:8] == b"\x89PNG\r\n\x1a\n"
    assert "ĐÚNG" in sv.cases_csv(rep).decode("utf-8-sig")
    assert sv.render_summary_png(rep)[:8] == b"\x89PNG\r\n\x1a\n"
    assert sv.render_timeline_png(rep["signals"], "T0", rep["trades"])[:8] == b"\x89PNG\r\n\x1a\n"
    assert sv.signals_csv(rep).decode("utf-8-sig").startswith("ngay,ma,thi_truong,ket_luan")

    # Dữ liệu bước ngẫu nhiên: không được "tìm ra" tác dụng giả.
    prices, idx = _regime_market(5, 5, trending=False)
    bt.FundamentalAsOf.get = lambda self, sym, day: strong
    try:
        data = bt.BacktestData(prices, idx, {}, {k: {"exchange": "HOSE"} for k in prices})
        sig, trades, _ = sv.replay_signals(data, start, end)
        rnd = sv.build_report(data, sig, trades, start, end, [], 0)
    finally:
        bt.FundamentalAsOf.get = saved
    assert abs(rnd["stats"]["ic"]["score"]) < 0.1, rnd["stats"]["ic"]
    assert all(icon not in ("✅", "❌") for icon, _ in rnd["verdicts"]), rnd["verdicts"]
    assert rnd["accuracy"]["overall"]["icon"] not in ("✅", "❌"), rnd["accuracy"]["overall"]
    assert "CHƯA đủ bằng chứng" in rnd["commentary"][-1] or "chưa đủ case" in rnd["commentary"][-1]


def main():
    assert STRATEGY.dist_window == 25 and STRATEGY.dist_threshold == 4
    tests = [test_market_gate, test_tier1, test_t21_delta_roc63, test_tier2_tier3, test_breakout_and_volume,
             test_evaluate_by_market, test_roe_ttm, test_vci_price_board_columns, test_source_order,
             test_cards_render, test_llm_context_grounding, test_multi_symbol_resolution,
             test_conversational_cards, test_conversation_router_and_fallback, test_agent_router_offline,
             test_chart_service, test_ui_cards, test_backtest_helpers, test_backtest_engine_rules,
             test_backtest_no_lookahead, test_backtest_matches_live_signal_and_explains, test_signal_validation]
    for fn in tests:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"ALL PASS ({len(tests)} nhóm)")


if __name__ == "__main__":
    main()
