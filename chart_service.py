"""Biểu đồ nến: ảnh PNG gửi Telegram và trang HTML tương tác kiểu sàn giao dịch.

Luồng: mã cổ phiếu -> OHLCV (stock_service.get_history) -> MA20/MA50/MA150,
Volume, RSI14 (ngưỡng 30/70) -> ảnh PNG (matplotlib) hoặc trang HTML
(TradingView Lightweight Charts).

Module này CHỈ trực quan hóa dữ liệu, không có quy tắc BUY/SELL nào.
RSI dùng đúng hàm strategy.wilder_rsi nên số trên biểu đồ khớp với engine.
"""
from __future__ import annotations

import html
import io
import json
import re
import threading
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter, MaxNLocator

import strategy
from utils import fmt_money, fmt_price, fmt_volume, padded, to_float

BASE_DIR = Path(__file__).resolve().parent
CHART_LIB_PATH = BASE_DIR / "static" / "lightweight-charts.standalone.production.js"
TEMPLATE_PATH = BASE_DIR / "templates" / "dashboard.html"

MA_WINDOWS = (20, 50, 150)
RSI_LOW, RSI_HIGH = 30, 70

# Khung hiển thị -> số phiên giao dịch (xấp xỉ). ALL = toàn bộ dữ liệu có.
PERIOD_BARS = {"1M": 22, "3M": 66, "6M": 126, "1Y": 252, "ALL": None}
PERIOD_LABELS = {"1M": "1 tháng", "3M": "3 tháng", "6M": "6 tháng", "1Y": "1 năm", "ALL": "Toàn bộ"}
PERIOD_SHORT = {"1M": "1T", "3M": "3T", "6M": "6T", "1Y": "1N", "ALL": "Tất cả"}
_PERIOD_ALIASES = {"1T": "1M", "3T": "3M", "6T": "6M", "1N": "1Y", "12M": "1Y", "12T": "1Y", "MAX": "ALL"}

# Bảng màu tối giống TradingView.
COLORS = {
    "bg": "#131722", "grid": "#1f2433", "text": "#d1d4dc", "muted": "#787b86",
    "up": "#26a69a", "down": "#ef5350",
    "ma20": "#f7c948", "ma50": "#2962ff", "ma150": "#e040fb", "rsi": "#b39ddb", "vol_ma": "#ff9800",
}

# matplotlib không an toàn khi nhiều thread cùng vẽ -> vẽ tuần tự.
_render_lock = threading.Lock()


# =====================================================================
# Khung thời gian + nhận diện câu chat
# =====================================================================
def parse_period_token(token) -> str | None:
    """'3T' -> '3M', '1n' -> '1Y'; không phải khung thời gian thì trả None."""
    raw = str(token or "").strip().upper()
    raw = _PERIOD_ALIASES.get(raw, raw)
    return raw if raw in PERIOD_BARS else None


def normalize_period(value, default: str = "6M") -> str:
    return parse_period_token(value) or parse_period_token(default) or "6M"


# "nến" bỏ dấu thành "nen", trùng với "nên" (nên mua), nên chỉ bắt các cụm rõ nghĩa.
CHART_KEYS = (
    " bieu do ", " do thi ", " chart ", " charts ", " candlestick ", " candle ",
    " hinh nen ", " ve nen ", " xem nen ", " nen nhat ", " dashboard ",
)


def is_chart_request(text: str) -> bool:
    q = padded(text)
    return any(k in q for k in CHART_KEYS)


def period_from_text(text: str) -> str | None:
    """'3 tháng' -> 3M, '1 năm' / '12 tháng' -> 1Y, 'tất cả' -> ALL. Không nhắc tới -> None."""
    q = padded(text)
    if any(k in q for k in (" tat ca ", " toan bo ", " all ", " max ")):
        return "ALL"
    if re.search(r" \d{1,2} ?(nam|n|y|year|years) ", q):
        return "1Y"
    m = re.search(r" (\d{1,2}) ?(thang|t|m|month|months) ", q)
    if m:
        months = int(m.group(1))
        return next((key for key, limit in (("1M", 1), ("3M", 3), ("6M", 6), ("1Y", 12)) if months <= limit), "ALL")
    if " nua nam " in q:
        return "6M"
    if " quy " in q:
        return "3M"
    return None


# =====================================================================
# Dữ liệu
# =====================================================================
def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Thêm MA20/50/150, RSI14, MA20 khối lượng, thay đổi so với phiên trước và GTGD."""
    x = df[["time", "open", "high", "low", "close", "volume"]].copy()
    x["time"] = pd.to_datetime(x["time"])
    for c in ("open", "high", "low", "close", "volume"):
        x[c] = pd.to_numeric(x[c], errors="coerce")
    x = x.dropna(subset=["time", "close"]).sort_values("time").reset_index(drop=True)

    # Một số nguồn thiếu open/high/low ở phiên cũ: lấp bằng close để nến không vỡ.
    for c in ("open", "high", "low"):
        x[c] = x[c].fillna(x["close"])
    x["high"] = x[["open", "high", "low", "close"]].max(axis=1)
    x["low"] = x[["open", "high", "low", "close"]].min(axis=1)
    x["volume"] = x["volume"].fillna(0)

    for n in MA_WINDOWS:
        x[f"ma{n}"] = x["close"].rolling(n, min_periods=n).mean()
    x["rsi14"] = strategy.wilder_rsi(x["close"], 14).to_numpy()
    x["vol_ma20"] = x["volume"].rolling(20, min_periods=20).mean()
    x["change"] = x["close"].diff()
    x["change_pct"] = x["change"] / x["close"].shift(1) * 100
    x["value"] = x["close"] * x["volume"]
    return x


def build_chart_frame(symbol: str) -> tuple[pd.DataFrame, dict]:
    """(bảng OHLCV + chỉ báo, thông tin mô tả). Dữ liệu đi qua cache + rate limiter của stock_service."""
    import stock_service as service  # import muộn để selftests không cần mạng

    symbol = symbol.upper().strip()
    hist, _, stale = service.get_history(symbol)
    if hist is None or hist.empty:
        raise service.DataUnavailable(f"Không có dữ liệu giá cho {symbol}")
    frame = compute_indicators(hist)

    try:
        meta = service.listing_map().get(symbol) or {}
    except Exception:
        meta = {}
    last_date = pd.Timestamp(frame["time"].iloc[-1]).date()
    info = {
        "symbol": symbol,
        "name": meta.get("name", ""),
        "exchange": "" if meta.get("exchange") in (None, "UNKNOWN") else meta["exchange"],
        "source": hist.attrs.get("source", ""),
        "stale": bool(stale),
        "live_bar": service.is_market_open_now() and last_date == service.now_vn().date(),
        "generated_at": datetime.now().strftime("%H:%M %d/%m/%Y"),
    }
    return frame, info


def latest_snapshot(frame: pd.DataFrame) -> dict:
    """Số liệu phiên cuối + thống kê 52 tuần, dùng cho caption và dashboard."""
    row = frame.iloc[-1]
    year = frame.tail(252)
    snap = {k: to_float(row.get(k)) for k in (
        "open", "high", "low", "close", "volume", "value", "change", "change_pct", "rsi14", "vol_ma20",
        *(f"ma{n}" for n in MA_WINDOWS))}
    snap["time"] = pd.Timestamp(row["time"])
    snap["high_52w"] = to_float(year["high"].max())
    snap["low_52w"] = to_float(year["low"].min())
    snap["vol_ratio"] = snap["volume"] / snap["vol_ma20"] if snap["volume"] is not None and snap["vol_ma20"] else None
    for n in MA_WINDOWS:
        ma = snap[f"ma{n}"]
        snap[f"dist_ma{n}"] = (snap["close"] / ma - 1) * 100 if ma and snap["close"] else None
    return snap


def rsi_zone(rsi) -> str:
    x = to_float(rsi)
    if x is None:
        return "N/A"
    if x >= RSI_HIGH:
        return f"vùng quá mua (≥{RSI_HIGH})"
    if x <= RSI_LOW:
        return f"vùng quá bán (≤{RSI_LOW})"
    return f"trung tính ({RSI_LOW}–{RSI_HIGH})"


# =====================================================================
# Caption Telegram
# =====================================================================
def chart_caption(frame: pd.DataFrame, info: dict, period: str) -> str:
    """Chú thích ảnh (HTML, < 1024 ký tự theo giới hạn Telegram). Chỉ số liệu, không khuyến nghị."""
    s = latest_snapshot(frame)
    chg, pct = s["change"], s["change_pct"]
    arrow = "🔺" if (chg or 0) > 0 else "🔻" if (chg or 0) < 0 else "▪️"
    change = "N/A" if chg is None else f"{arrow} {chg:+,.0f} ({pct:+.2f}%)"
    live = " · <i>nến đang chạy</i>" if info.get("live_bar") else ""

    def dist(key: str) -> str:
        x = s[f"dist_{key}"]
        return "" if x is None else f" · giá {x:+.1f}%"

    vol_ratio = f" · {s['vol_ratio']:.1f}× TB20" if s["vol_ratio"] else ""
    from_high = (s["close"] / s["high_52w"] - 1) * 100 if s["high_52w"] else None
    foot = [f"nguồn {html.escape(info['source'])}"] if info.get("source") else []
    if info.get("stale"):
        foot.append("⚠️ cache cũ")
    foot.append("chỉ trực quan hóa, không phải tín hiệu mua/bán")
    lines = [
        f"📈 <b>{html.escape(info['symbol'])}</b> · {PERIOD_LABELS[period]} · khung ngày{live}",
        f"💰 <b>{fmt_price(s['close'])}</b> {change} · {s['time']:%d/%m/%Y}",
        f"🕯 O {fmt_price(s['open'])} · H {fmt_price(s['high'])} · L {fmt_price(s['low'])}",
        f"📦 KL {fmt_volume(s['volume'])}{vol_ratio} · 💵 {fmt_money(s['value'])}",
        f"🟨 MA20 {fmt_price(s['ma20'])}{dist('ma20')}",
        f"🟦 MA50 {fmt_price(s['ma50'])}{dist('ma50')}",
        f"🟪 MA150 {fmt_price(s['ma150'])}{dist('ma150')}",
        f"📊 RSI14 {rsi_zone_icon(s['rsi14'])}",
        f"🏔 52 tuần {fmt_price(s['low_52w'])} – {fmt_price(s['high_52w'])}"
        + ("" if from_high is None else f" · cách đỉnh {from_high:+.1f}%"),
        "<i>" + " · ".join(foot) + "</i>",
    ]
    return "\n".join(lines)[:1020]


def rsi_zone_icon(rsi) -> str:
    x = to_float(rsi)
    if x is None:
        return "N/A"
    icon = "🔥" if x >= RSI_HIGH else "🧊" if x <= RSI_LOW else "⚖️"
    return f"{x:.1f} {icon} {rsi_zone(x)}"


# =====================================================================
# Ảnh PNG (matplotlib)
# =====================================================================
def _axis_compact(value, _pos=None) -> str:
    a = abs(value)
    for size, suffix in ((1e9, "B"), (1e6, "M")):
        if a >= size:
            return f"{value / size:.1f}{suffix}"
    return f"{value / 1e3:.0f}K" if a >= 1e3 else f"{value:.0f}"


def _style_axis(ax) -> None:
    ax.set_facecolor(COLORS["bg"])
    ax.grid(True, color=COLORS["grid"], linewidth=0.6)
    ax.tick_params(colors=COLORS["muted"], labelsize=8, length=0)
    ax.yaxis.tick_right()
    for spine in ax.spines.values():
        spine.set_color(COLORS["grid"])


def _draw_price(ax, v: pd.DataFrame, frame: pd.DataFrame, info: dict, width: float) -> None:
    x = np.arange(len(v))
    o, h, lo, c = (v[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    colors = np.where(c >= o, COLORS["up"], COLORS["down"])

    # Nến: bấc là đường dọc low-high, thân là cột open-close (thân quá mỏng được nới để còn nhìn thấy).
    ax.vlines(x, lo, h, colors=colors, linewidth=0.9 if len(v) <= 160 else 0.6, zorder=2)
    span = max(np.nanmax(h) - np.nanmin(lo), 1.0)
    body = np.maximum(np.abs(c - o), span * 0.0015)
    ax.bar(x, body, bottom=np.minimum(o, c), width=width, color=colors, edgecolor=colors, linewidth=0.4, zorder=3)

    handles = []
    for n in MA_WINDOWS:
        label = f"MA{n} {fmt_price(frame[f'ma{n}'].iloc[-1])}"
        (line,) = ax.plot(x, v[f"ma{n}"].to_numpy(float), color=COLORS[f"ma{n}"], linewidth=1.2, label=label, zorder=4)
        handles.append(line)
    ax.legend(handles=handles, loc="upper left", frameon=False, fontsize=8.5, labelcolor="linecolor",
              ncol=3, handlelength=1.4, borderaxespad=0.3)

    # Trục giá co giãn theo vùng đang xem (tính cả các đường MA).
    values = np.concatenate([lo, h] + [v[f"ma{n}"].dropna().to_numpy(float) for n in MA_WINDOWS])
    vmin, vmax = np.nanmin(values), np.nanmax(values)
    pad = (vmax - vmin) * 0.06 or vmax * 0.02
    ax.set_ylim(vmin - pad, vmax + pad * 1.6)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda val, _p: f"{val:,.0f}"))
    ax.yaxis.set_major_locator(MaxNLocator(7))

    ax.text(0.5, 0.5, info["symbol"], transform=ax.transAxes, ha="center", va="center",
            fontsize=64, color="#ffffff", alpha=0.04, fontweight="bold", zorder=0)

    # Nhãn giá đóng cửa cuối ở mép phải, giống bảng giá trên sàn.
    last_color = colors[-1]
    ax.axhline(c[-1], color=last_color, linewidth=0.7, linestyle=(0, (2, 3)), zorder=1)
    ax.annotate(f"{c[-1]:,.0f}", xy=(1.0, c[-1]), xycoords=("axes fraction", "data"), xytext=(3, 0),
                textcoords="offset points", va="center", ha="left", fontsize=8, color="white",
                fontweight="bold", bbox={"boxstyle": "square,pad=0.25", "fc": last_color, "ec": last_color},
                zorder=7, annotation_clip=False)


def _draw_volume(ax, v: pd.DataFrame, width: float) -> None:
    x = np.arange(len(v))
    vol = v["volume"].to_numpy(float)
    colors = np.where(v["close"] >= v["open"], COLORS["up"], COLORS["down"])
    ax.bar(x, vol, width=width, color=colors, alpha=0.75, zorder=2)
    ax.plot(x, v["vol_ma20"].to_numpy(float), color=COLORS["vol_ma"], linewidth=1.0, zorder=3)
    ax.set_ylim(0, max(np.nanmax(vol), 1) * 1.35)
    ax.yaxis.set_major_formatter(FuncFormatter(_axis_compact))
    ax.yaxis.set_major_locator(MaxNLocator(3))
    ax.text(0.005, 0.93, f"Volume {fmt_volume(vol[-1])}  ·  MA20 KL {fmt_volume(v['vol_ma20'].iloc[-1])}",
            transform=ax.transAxes, fontsize=8, color=COLORS["text"], va="top")


def _draw_rsi(ax, v: pd.DataFrame, frame: pd.DataFrame) -> None:
    x = np.arange(len(v))
    rsi = v["rsi14"].to_numpy(float)
    ax.plot(x, rsi, color=COLORS["rsi"], linewidth=1.1, zorder=3)
    ax.axhspan(RSI_LOW, RSI_HIGH, color=COLORS["rsi"], alpha=0.06, zorder=1)
    for level in (RSI_LOW, RSI_HIGH):
        ax.axhline(level, color=COLORS["muted"], linewidth=0.8, linestyle=(0, (4, 3)), zorder=2)
    ax.axhline(50, color=COLORS["grid"], linewidth=0.7, zorder=1)
    with np.errstate(invalid="ignore"):   # RSI có NaN ở 14 phiên đầu
        ax.fill_between(x, rsi, RSI_HIGH, where=rsi >= RSI_HIGH, color=COLORS["down"], alpha=0.25, interpolate=True)
        ax.fill_between(x, rsi, RSI_LOW, where=rsi <= RSI_LOW, color=COLORS["up"], alpha=0.25, interpolate=True)
    ax.set_ylim(0, 100)
    ax.set_yticks([RSI_LOW, 50, RSI_HIGH])
    last = to_float(frame["rsi14"].iloc[-1])
    ax.text(0.005, 0.93, f"RSI14 {'N/A' if last is None else f'{last:.1f}'}  ·  ngưỡng {RSI_LOW}/{RSI_HIGH}",
            transform=ax.transAxes, fontsize=8, color=COLORS["text"], va="top")


def _draw_header(fig, frame: pd.DataFrame, info: dict, period: str) -> None:
    s = latest_snapshot(frame)
    chg, pct = s["change"] or 0.0, s["change_pct"] or 0.0
    color = COLORS["up"] if chg > 0 else COLORS["down"] if chg < 0 else COLORS["ma20"]
    title = fig.text(0.035, 0.955, info["symbol"], fontsize=17, fontweight="bold", color=COLORS["text"], va="center")
    # Đặt giá ngay sau mã: đo độ rộng thực tế của chữ mã.
    right = title.get_window_extent(renderer=fig.canvas.get_renderer()).x1
    x_after = fig.transFigure.inverted().transform((right, 0))[0] + 0.012
    fig.text(x_after, 0.955, f"{s['close']:,.0f}   {chg:+,.0f} ({pct:+.2f}%)", fontsize=13, color=color,
             fontweight="bold", va="center")
    sub = " · ".join(p for p in (info.get("exchange"), "Khung ngày", PERIOD_LABELS[period],
                                 f"Nguồn {info['source']}" if info.get("source") else "") if p)
    fig.text(0.035, 0.915, f"{s['time']:%d/%m/%Y}  ·  O {fmt_price(s['open'])}  H {fmt_price(s['high'])}  "
                           f"L {fmt_price(s['low'])}  C {fmt_price(s['close'])}  ·  {sub}",
             fontsize=8.5, color=COLORS["muted"], va="center")
    fig.text(0.925, 0.02, "Chỉ trực quan hóa dữ liệu — không phải tín hiệu mua/bán",
             fontsize=7, color=COLORS["muted"], ha="right")


def render_chart_png(frame: pd.DataFrame, info: dict, period: str = "6M") -> bytes:
    """Ảnh 3 tầng: nến + MA20/50/150 | Volume + MA20 KL | RSI14 với ngưỡng 30/70."""
    period = normalize_period(period)
    bars = PERIOD_BARS[period]
    v = (frame.tail(bars) if bars else frame).reset_index(drop=True)
    if v.empty:
        raise ValueError("Không có dữ liệu để vẽ")
    n = len(v)
    width = 0.62 if n <= 160 else 0.7

    with _render_lock:
        fig = Figure(figsize=(12, 7.6), dpi=150, facecolor=COLORS["bg"])
        FigureCanvasAgg(fig)
        grid = fig.add_gridspec(3, 1, height_ratios=[5.2, 1.5, 1.5], hspace=0.06,
                                left=0.035, right=0.925, top=0.885, bottom=0.07)
        ax_price = fig.add_subplot(grid[0])
        ax_vol = fig.add_subplot(grid[1], sharex=ax_price)
        ax_rsi = fig.add_subplot(grid[2], sharex=ax_price)
        for ax in (ax_price, ax_vol, ax_rsi):
            _style_axis(ax)

        _draw_price(ax_price, v, frame, info, width)
        _draw_volume(ax_vol, v, width)
        _draw_rsi(ax_rsi, v, frame)

        # Trục X dùng số thứ tự phiên (bỏ qua ngày nghỉ), nhãn là ngày tương ứng.
        dates = pd.to_datetime(v["time"])
        date_format = "%d/%m" if n <= 160 else "%m/%Y"
        ax_rsi.xaxis.set_major_locator(MaxNLocator(8, integer=True))
        ax_rsi.xaxis.set_major_formatter(FuncFormatter(
            lambda val, _p: dates.iloc[int(round(val))].strftime(date_format) if 0 <= round(val) < n else ""))
        ax_price.tick_params(labelbottom=False)
        ax_vol.tick_params(labelbottom=False)
        ax_price.set_xlim(-1, n + 1.5)

        _draw_header(fig, frame, info, period)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=COLORS["bg"])
    return buf.getvalue()


# =====================================================================
# Trang HTML tương tác
# =====================================================================
def _column(series: pd.Series, digits: int) -> list:
    """Cột số -> list JSON (NaN thành null)."""
    values = (to_float(v) for v in series)
    return [None if x is None else round(x, digits) for x in values]


def dashboard_payload(frame: pd.DataFrame, info: dict, period: str = "6M") -> dict:
    """Dữ liệu nhúng vào trang HTML: toàn bộ lịch sử (để MA150 có ngay từ đầu vùng xem) + thống kê."""
    s = latest_snapshot(frame)
    return {
        **{k: info.get(k, "") for k in ("symbol", "name", "exchange", "source", "generated_at")},
        "stale": bool(info.get("stale")),
        "live_bar": bool(info.get("live_bar")),
        "period": normalize_period(period),
        "rsi_low": RSI_LOW,
        "rsi_high": RSI_HIGH,
        "t": [f"{t:%Y-%m-%d}" for t in pd.to_datetime(frame["time"])],
        "o": _column(frame["open"], 2), "h": _column(frame["high"], 2),
        "l": _column(frame["low"], 2), "c": _column(frame["close"], 2),
        "v": _column(frame["volume"], 0), "vma20": _column(frame["vol_ma20"], 0),
        "ma20": _column(frame["ma20"], 2), "ma50": _column(frame["ma50"], 2),
        "ma150": _column(frame["ma150"], 2), "rsi": _column(frame["rsi14"], 2),
        "stats": {k: s[k] for k in ("high_52w", "low_52w", "vol_ma20", "vol_ratio", "rsi14",
                                    "dist_ma20", "dist_ma50", "dist_ma150")},
    }


def render_dashboard_html(frame: pd.DataFrame, info: dict, period: str = "6M", *, server_mode: bool = False) -> str:
    """Trang HTML tự chứa: thư viện biểu đồ và dữ liệu đều nhúng sẵn, mở được cả khi không có mạng.

    server_mode=True: bật ô tìm mã và nạp script Telegram Web App (khi phục vụ qua dashboard_server).
    """
    payload = dashboard_payload(frame, info, period) | {"server_mode": server_mode}
    # Chống chuỗi '</script' trong dữ liệu làm đóng thẻ <script> sớm.
    data_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    chart_lib = CHART_LIB_PATH.read_text(encoding="utf-8").replace("</script", "<\\/script")
    telegram_js = '<script src="https://telegram.org/js/telegram-web-app.js" async></script>' if server_mode else ""
    return (TEMPLATE_PATH.read_text(encoding="utf-8")
            .replace("__TITLE__", html.escape(f"{payload['symbol']} · Biểu đồ nến"))
            .replace("__TG_SCRIPT__", telegram_js)
            .replace("__LWC_JS__", chart_lib)
            .replace("__DATA_JSON__", data_json))
