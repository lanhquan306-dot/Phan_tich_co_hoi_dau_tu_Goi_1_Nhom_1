"""Telegram bot: điểm chạy chính của dự án (python bot.py).

Nhận tin nhắn/lệnh từ Telegram, gọi các tầng bên dưới và gửi kết quả về:
    - Chat tự nhiên    -> llm.run_financial_agent -> presentation (thẻ HTML)
    - "biểu đồ FPT"    -> chart_service (ảnh PNG + nút mở biểu đồ tương tác)
    - Lệnh /signal, /quote, /chart, /market, /top, /watch, /position...
    - "backtest", /backtest, nút 🧪 -> backtest.py (kiểm định chiến lược trên dữ liệu quá khứ)

Watchlist, vị thế và bộ nhớ hội thoại chỉ lưu trong RAM: khởi động lại bot là mất.
"""
from __future__ import annotations

import asyncio
import html
import io
import logging
import re
import sys
from datetime import date

from telegram import (
    BotCommand,
    CallbackQuery,
    Message,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    InputMediaPhoto,
    Update,
    WebAppInfo,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

import backtest as bt
import chart_service as charts
import presentation as ui
import signal_validation as sv
from config import (
    ALERT_SECONDS,
    BACKTEST,
    VALIDATION,
    CHART_DEFAULT_PERIOD,
    DASHBOARD_ENABLED,
    DASHBOARD_HOST,
    DASHBOARD_PORT,
    DASHBOARD_PUBLIC_URL,
    TELEGRAM_BOT_TOKEN,
    TOP_SYMBOLS,
)
from dashboard_server import start_dashboard_server
from llm import explain_stock, run_financial_agent
from stock_service import VnstockRateLimited, analyze_symbol, get_quote, resolve_symbol, resolve_symbols
from utils import normalize_text, padded

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("fintech-bot")

# Trạng thái trong RAM: {chat_id: {mã: ...}}.
WATCHLIST: dict[int, dict[str, str | None]] = {}   # mã -> tín hiệu lần kiểm tra trước
POSITIONS: dict[int, dict[str, dict]] = {}         # mã -> vị thế mô phỏng

TELEGRAM_TEXT_LIMIT = 4000
CHART_PERIOD_BUTTONS = ("1M", "3M", "6M", "1Y")
DEFAULT_CHART_PERIOD = charts.normalize_period(CHART_DEFAULT_PERIOD)
GREETINGS = {"hi", "hello", "alo", "hey", "ok", "xin chao", "chao", "hello bot"}
RATE_LIMIT_TEXT = "⚠️ VNStock đang chạm quota. Bot vẫn chạy; thử lại sau khoảng 1 phút."

WELCOME = """🤖 <b>FINTECH COPILOT</b> · Trợ lý cổ phiếu HOSE/HNX
━━━━━━━━━━━━━━━━━━
Bạn <b>không cần nhớ lệnh</b>, cứ hỏi tự nhiên:

🧠 <b>Phân tích</b>   “Cổ phiếu OCB thế nào?”
❓ <b>Hỏi tiếp</b>     “Tại sao chỉ 30 điểm?” · “T3 của nó vì sao bằng 0?”
⚖️ <b>So sánh</b>     “OCB so với VIC thì sao?”
💹 <b>Giá</b>             “Giá nó hiện bao nhiêu?”
🏛 <b>Thị trường</b> “Thị trường hôm nay thế nào?”
📈 <b>Biểu đồ</b>      “Biểu đồ FPT” · “chart HPG 1 năm”
🧪 <b>Backtest</b>     “backtest 6 tháng” · “backtest FPT HPG”
🔬 <b>Kiểm định</b>   “kiểm định tín hiệu FPT” · “test quá khứ FPT 6 tháng”

⌨️ <b>Lệnh nhanh</b>
/signal FPT — chấm điểm chi tiết
/chart FPT 3M — biểu đồ nến
/quote · /market · /top
/watch FPT · /watchlist — theo dõi tín hiệu
/position add FPT 100000 · /positions — vị thế mô phỏng
/backtest · /backtest FPT 3M — backtest danh mục (vốn 100tr, 5 vị thế)
/kiemdinh FPT · /kiemdinh FPT 6M 10 — bot đúng bao nhiêu case trong quá khứ

⚙️ <b>Cách bot làm việc</b>
• <b>Rule engine</b> tính giá, chỉ báo, điểm T1/T2/T3 và BUY/HOLD/SELL.
• <b>Copilot</b> hiểu câu hỏi, nhớ ngữ cảnh và diễn giải; AI không được đổi số liệu.
<i>ℹ️ Tham khảo cho mục đích học tập, không phải tư vấn đầu tư.</i>
"""


# =====================================================================
# Tiện ích chung
# =====================================================================
def position_for(chat_id: int, symbol: str) -> dict | None:
    return POSITIONS.get(chat_id, {}).get(symbol.upper())


def _history(context) -> list[dict]:
    return context.chat_data.setdefault("history", [])


def _remember(context, role: str, text: str) -> None:
    """Lưu 8 lượt hội thoại gần nhất để hiểu câu hỏi nối tiếp."""
    if text:
        history = _history(context)
        history.append({"role": role, "text": str(text)[:1200]})
        del history[:-8]


def _remember_symbols(context, symbols) -> None:
    """Lưu 5 mã được nhắc gần nhất, mã mới nhất ở cuối."""
    recent = context.chat_data.setdefault("recent_symbols", [])
    for symbol in (str(s).upper().strip() for s in symbols or []):
        if symbol:
            if symbol in recent:
                recent.remove(symbol)
            recent.append(symbol)
    del recent[:-5]
    if recent:
        context.chat_data["last_symbol"] = recent[-1]


async def _reply(target: Update | Message | CallbackQuery, text: str, markup=None) -> None:
    """CallbackQuery: sửa tin nhắn chứa nút vừa bấm. Update/Message: gửi tin trả lời. Nội dung dạng HTML."""
    if isinstance(target, CallbackQuery):
        await target.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return
    message = target.message if isinstance(target, Update) else target
    await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def _resolve_arg(update: Update, raw: str, action: str = "analyze") -> str | None:
    """Nhận diện mã từ tham số lệnh; sai mã thì gửi gợi ý và trả None."""
    resolution = await asyncio.to_thread(resolve_symbol, raw)
    if resolution["status"] == "ok":
        return resolution["symbol"]
    typed = html.escape(resolution.get("typed", raw))
    suggestions = resolution.get("suggestions") or []
    if not suggestions:
        await update.message.reply_text(f"Không tìm thấy mã <b>{typed}</b> trên HOSE/HNX.", parse_mode=ParseMode.HTML)
        return None
    callback = (lambda s: f"chart:{s}:{DEFAULT_CHART_PERIOD}") if action == "chart" else (lambda s: f"analyze:{s}")
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(s, callback_data=callback(s)) for s in suggestions]])
    await update.message.reply_text(f"Không tìm thấy mã <b>{typed}</b>.\nBạn muốn hỏi mã nào?",
                                    parse_mode=ParseMode.HTML, reply_markup=keyboard)
    return None


# =====================================================================
# Biểu đồ nến
# =====================================================================
def dashboard_url(symbol: str, period: str) -> str | None:
    return f"{DASHBOARD_PUBLIC_URL}/analysis/{symbol}" if DASHBOARD_PUBLIC_URL else None


def chart_keyboard(symbol: str, period: str, chat_type: str | None, *, allow_link: bool = True):
    """Hàng 1: đổi khung thời gian. Hàng 2: mở biểu đồ tương tác.

    - Có DASHBOARD_PUBLIC_URL dạng https + chat riêng -> Web App, mở ngay trong Telegram.
    - Có DASHBOARD_PUBLIC_URL khác (group, http)       -> nút link mở trình duyệt.
    - Không có link                                    -> bot gửi file HTML tương tác.
    """
    periods = [
        InlineKeyboardButton(f"• {charts.PERIOD_SHORT[p]} •" if p == period else charts.PERIOD_SHORT[p],
                             callback_data=f"chart:{symbol}:{p}")
        for p in CHART_PERIOD_BUTTONS
    ]
    label, file_callback = "🔍 Mở dashboard phân tích", f"chartfile:{symbol}:{period}"
    url = dashboard_url(symbol, period) if allow_link else None
    if not url:
        return InlineKeyboardMarkup([periods, [InlineKeyboardButton(label, callback_data=file_callback)]])
    if url.startswith("https://") and chat_type == "private":
        open_button = InlineKeyboardButton(label, web_app=WebAppInfo(url=url))
    else:
        open_button = InlineKeyboardButton(label, url=url)
    file_button = InlineKeyboardButton("📄 File HTML", callback_data=file_callback)
    return InlineKeyboardMarkup([periods, [open_button, file_button]])


def actions_keyboard(symbols, *, detail: bool = True) -> InlineKeyboardMarkup | None:
    """Nút hành động dưới thẻ trả lời.

    Một mã : [📈 Biểu đồ] [📋 Chi tiết] [🔔 Theo dõi]
    Nhiều mã: một hàng 📈 cho từng mã, một hàng 📋 cho từng mã (tối đa 3 mã).
    """
    symbols = list(symbols or [])[:3]
    if not symbols:
        return None

    def chart(s, label):
        return InlineKeyboardButton(label, callback_data=f"chart:{s}:{DEFAULT_CHART_PERIOD}")

    def info(s, label):
        return InlineKeyboardButton(label, callback_data=f"detail:{s}")

    if len(symbols) == 1:
        s = symbols[0]
        row = [chart(s, "📈 Biểu đồ")] + ([info(s, "📋 Chi tiết")] if detail else [])
        return InlineKeyboardMarkup([
            row + [InlineKeyboardButton("🔔 Theo dõi", callback_data=f"watch:{s}")],
            [InlineKeyboardButton(f"🧪 Backtest {s}", callback_data=f"bt:{s}:6M"),
             InlineKeyboardButton(f"🔬 Độ chính xác {s} (6 tháng)", callback_data=f"vd:{s}:6M")],
        ])
    rows = [[chart(s, f"📈 {s}") for s in symbols]]
    if detail:
        rows.append([info(s, f"📋 {s}") for s in symbols])
    rows.append([InlineKeyboardButton(f"🧪 Backtest {len(symbols)} mã", callback_data=f"bt:{','.join(symbols)}:6M"),
                 InlineKeyboardButton(f"🔬 Độ chính xác {len(symbols)} mã", callback_data=f"vd:{','.join(symbols)}:6M")])
    return InlineKeyboardMarkup(rows)


def _chart_error_text(symbol: str, exc: Exception) -> str:
    if isinstance(exc, VnstockRateLimited):
        return RATE_LIMIT_TEXT
    log.exception("chart %s", symbol)
    return (f"Không vẽ được biểu đồ <b>{html.escape(symbol)}</b> lúc này.\n"
            f"<code>{html.escape(type(exc).__name__)}: {html.escape(str(exc)[:200])}</code>")


def _render_chart(symbol: str, period: str) -> tuple[bytes, str]:
    frame, info = charts.build_chart_frame(symbol)
    return charts.render_chart_png(frame, info, period), charts.chart_caption(frame, info, period)


async def send_chart(context, chat_id: int, chat_type: str | None, symbol: str, period: str | None = None,
                     *, query=None) -> None:
    """Gửi ảnh biểu đồ kèm nút. Nếu `query` là nút bấm trên chính ảnh cũ thì thay ảnh tại chỗ."""
    symbol = symbol.upper()
    period = charts.normalize_period(period, DEFAULT_CHART_PERIOD)
    _remember_symbols(context, [symbol])
    try:
        await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_PHOTO)
        png, caption = await asyncio.to_thread(_render_chart, symbol, period)
    except Exception as exc:
        await context.bot.send_message(chat_id, _chart_error_text(symbol, exc), parse_mode=ParseMode.HTML)
        return

    edit_in_place = bool(query and query.message and query.message.photo)

    async def deliver(allow_link: bool) -> None:
        keyboard = chart_keyboard(symbol, period, chat_type, allow_link=allow_link)
        if edit_in_place:
            await query.edit_message_media(InputMediaPhoto(png, caption=caption, parse_mode=ParseMode.HTML),
                                           reply_markup=keyboard)
        else:
            await context.bot.send_photo(chat_id, photo=png, caption=caption, parse_mode=ParseMode.HTML,
                                         reply_markup=keyboard)

    try:
        await deliver(allow_link=True)
    except BadRequest as exc:
        message = str(exc).lower()
        if "not modified" in message:          # bấm lại đúng khung đang xem
            return
        if not dashboard_url(symbol, period) or not any(k in message for k in ("url", "button", "web app")):
            raise
        # Telegram từ chối link (VD localhost, IP nội bộ) -> dùng nút gửi file HTML.
        log.warning("Telegram từ chối link dashboard (%s); dùng nút file HTML.", exc)
        await deliver(allow_link=False)


async def send_chart_file(context, chat_id: int, symbol: str, period: str) -> None:
    """Gửi trang HTML tự chứa: mở bằng Chrome/Safari để rê chuột xem chi tiết từng nến."""
    symbol = symbol.upper()
    period = charts.normalize_period(period, DEFAULT_CHART_PERIOD)

    def build() -> str:
        frame, info = charts.build_chart_frame(symbol)
        return charts.render_dashboard_html(frame, info, period)

    try:
        await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
        page = await asyncio.to_thread(build)
    except Exception as exc:
        await context.bot.send_message(chat_id, _chart_error_text(symbol, exc), parse_mode=ParseMode.HTML)
        return
    await context.bot.send_document(
        chat_id,
        document=InputFile(io.BytesIO(page.encode("utf-8")), filename=f"{symbol}_bieu_do_{date.today():%Y%m%d}.html"),
        caption=(f"📊 <b>{html.escape(symbol)}</b> — biểu đồ tương tác.\n"
                 "Mở file bằng Chrome/Safari: rê chuột (hoặc chạm giữ trên điện thoại) vào nến để xem OHLC, "
                 "thay đổi, khối lượng, GTGD, MA20/50/150, RSI14. Có nút 1T/3T/6T/1N và bật/tắt MA."),
        parse_mode=ParseMode.HTML,
    )


# =====================================================================
# Lệnh
# =====================================================================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🧪 Backtest & 🔬 kiểm định", callback_data="btmenu:*"),
         InlineKeyboardButton("🏛 Thị trường", callback_data="menu:market")],
    ])
    await update.message.reply_text(WELCOME, parse_mode=ParseMode.HTML, reply_markup=keyboard)


async def send_signal(target, context, symbol: str, chat_id: int, *, with_ai: bool = False) -> None:
    """Thẻ /signal đầy đủ (+ đoạn AI giải thích nếu có Groq). `target` là Update hoặc CallbackQuery."""
    try:
        await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
        result = await asyncio.to_thread(analyze_symbol, symbol, position_for(chat_id, symbol))
    except VnstockRateLimited:
        await _reply(target, RATE_LIMIT_TEXT)
        return
    except Exception as exc:
        log.exception("analyze %s", symbol)
        await _reply(target, f"Không phân tích được <b>{html.escape(symbol)}</b> lúc này.\n"
                             f"<code>{html.escape(type(exc).__name__)}: {html.escape(str(exc)[:250])}</code>")
        return

    _remember_symbols(context, [symbol])
    await _reply(target, ui.signal_card(result), actions_keyboard([symbol], detail=False))
    if with_ai:
        question = f"Giải thích tín hiệu {symbol} hiện tại và hành động theo chiến lược của bot."
        ai_text = await asyncio.to_thread(explain_stock, question, result, _history(context))
        if ai_text:
            await context.bot.send_message(chat_id, ai_text[:TELEGRAM_TEXT_LIMIT])
            _remember(context, "assistant", ai_text)


async def signal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Cú pháp: /signal FPT")
        return
    symbol = await _resolve_arg(update, context.args[0])
    if symbol:
        await send_signal(update, context, symbol, update.effective_chat.id, with_ai=True)


async def quote_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Cú pháp: /quote FPT")
        return
    symbol = await _resolve_arg(update, context.args[0])
    if not symbol:
        return
    _remember_symbols(context, [symbol])
    try:
        q, _, stale = await asyncio.to_thread(get_quote, symbol)
    except VnstockRateLimited:
        await update.message.reply_text(RATE_LIMIT_TEXT)
        return
    except Exception as exc:
        await update.message.reply_text(f"Không lấy được giá {symbol}: {exc}")
        return
    text = ui.quote_card(symbol, dict(q, stale_cache=stale))
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=actions_keyboard([symbol]))


async def chart_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/chart FPT, /chart FPT 3M, /chart 1Y HPG; không có mã thì dùng mã vừa hỏi."""
    raw_symbol, period = None, None
    for arg in context.args or []:
        if charts.parse_period_token(arg):
            period = charts.parse_period_token(arg)
        elif raw_symbol is None:
            raw_symbol = arg
    chat = update.effective_chat
    symbol = await _resolve_arg(update, raw_symbol, "chart") if raw_symbol else context.chat_data.get("last_symbol")
    if symbol:
        await send_chart(context, chat.id, chat.type, symbol, period)
    elif not raw_symbol:
        await update.message.reply_text("Cú pháp: /chart FPT  (thêm 1M, 3M, 6M, 1Y hoặc ALL nếu muốn)")


async def market_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(await asyncio.to_thread(ui.market_card), parse_mode=ParseMode.HTML)


async def top_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Quét các mã trong TOP_SYMBOLS và xếp: BUY trước, điểm cao trước, Delta ROC63 cao trước."""
    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
    rows = []
    for symbol in TOP_SYMBOLS:
        try:
            rows.append(await asyncio.to_thread(analyze_symbol, symbol, position_for(chat_id, symbol)))
        except Exception as exc:
            log.warning("/top bỏ qua %s: %s", symbol, exc)
    if not rows:
        await update.message.reply_text("Không quét được mã nào. Có thể VNStock đang chạm quota.")
        return

    order = {"BUY": 0, "HOLD": 1, "SELL": 2}

    def rank(r: dict):
        delta = (r.get("tech") or {}).get("delta_roc63")
        return order.get(r["signal"], 9), -(r.get("score") or 0), -(delta if delta is not None else -1e9), r["symbol"]

    rows.sort(key=rank)
    await update.message.reply_text(ui.top_card(rows)[:TELEGRAM_TEXT_LIMIT], parse_mode=ParseMode.HTML,
                                    reply_markup=actions_keyboard([r["symbol"] for r in rows]))


async def watch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Cú pháp: /watch FPT")
        return
    symbol = await _resolve_arg(update, context.args[0])
    if symbol:
        WATCHLIST.setdefault(update.effective_chat.id, {})[symbol] = None
        await update.message.reply_text(f"🔔 Đã theo dõi {symbol}. Bot kiểm tra khoảng mỗi {ALERT_SECONDS // 60} phút "
                                        "và chỉ báo khi tín hiệu đổi.")


async def unwatch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Cú pháp: /unwatch FPT")
        return
    symbol = context.args[0].upper()
    WATCHLIST.get(update.effective_chat.id, {}).pop(symbol, None)
    await update.message.reply_text(f"🔕 Đã bỏ theo dõi {symbol}.")


async def watchlist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    items = WATCHLIST.get(update.effective_chat.id, {})
    if not items:
        await update.message.reply_text("🔕 Watchlist trống. Thêm mã bằng /watch FPT.")
        return
    await update.message.reply_text(ui.watchlist_card(items, ALERT_SECONDS // 60), parse_mode=ParseMode.HTML)


async def position_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/position add FPT 100000 | /position del FPT — vị thế mô phỏng để engine xét điều kiện SELL."""
    args, chat_id = context.args or [], update.effective_chat.id
    action = args[0].lower() if args else ""
    if action == "add" and len(args) >= 3:
        symbol = args[1].upper()
        try:
            price = float(args[2].replace(",", ""))
        except ValueError:
            await update.message.reply_text("Giá mua không hợp lệ.")
            return
        POSITIONS.setdefault(chat_id, {})[symbol] = {
            "symbol": symbol, "entry_price": price, "entry_date": date.today().isoformat(), "peak_close": price,
        }
        await update.message.reply_text(f"💼 Đã lưu vị thế {symbol} @ {price:,.0f}. Xem lãi/lỗ bằng /positions.")
    elif action in {"del", "delete", "remove"} and len(args) >= 2:
        symbol = args[1].upper()
        POSITIONS.get(chat_id, {}).pop(symbol, None)
        await update.message.reply_text(f"🗑 Đã xóa vị thế {symbol}.")
    else:
        await update.message.reply_text("Cú pháp:\n/position add FPT 100000\n/position del FPT")


async def positions_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    items = POSITIONS.get(update.effective_chat.id, {})
    if not items:
        await update.message.reply_text("💼 Chưa có vị thế mô phỏng. Thêm bằng /position add FPT 100000.")
        return

    def latest_price(symbol: str):
        try:
            return get_quote(symbol)[0].get("price")
        except Exception:
            return None

    prices = {s: await asyncio.to_thread(latest_price, s) for s in items}
    await update.message.reply_text(ui.positions_card(items, prices), parse_mode=ParseMode.HTML,
                                    reply_markup=actions_keyboard(list(items), detail=True))


# =====================================================================
# Nút bấm (callback)
# =====================================================================
async def analyze_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Nút gợi ý mã khi gõ sai: callback_data = 'analyze:<MÃ>'."""
    query = update.callback_query
    await query.answer()
    await send_signal(query, context, query.data.split(":", 1)[1].upper(), query.message.chat_id)


async def detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Nút “📋 Chi tiết”: gửi thẻ /signal thành tin nhắn mới, giữ nguyên thẻ đang xem."""
    query = update.callback_query
    symbol = query.data.split(":", 1)[1].upper()
    await query.answer(f"Đang chấm điểm {symbol}…")
    await send_signal(query.message, context, symbol, query.message.chat_id)


async def watch_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Nút “🔔 Theo dõi”."""
    query = update.callback_query
    symbol = query.data.split(":", 1)[1].upper()
    WATCHLIST.setdefault(query.message.chat_id, {}).setdefault(symbol, None)
    await query.answer(f"🔔 Đã theo dõi {symbol}. Bot sẽ báo khi tín hiệu đổi.")


async def chart_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Nút đổi khung / nút “📈 Biểu đồ”: callback_data = 'chart:<MÃ>:<KHUNG>'."""
    query = update.callback_query
    _, symbol, *rest = query.data.split(":")
    period = rest[0] if rest else DEFAULT_CHART_PERIOD
    await query.answer(f"Đang vẽ {symbol} {charts.PERIOD_LABELS.get(period, '')}…")
    chat = query.message.chat
    await send_chart(context, chat.id, chat.type, symbol, period, query=query)


async def chart_file_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Nút mở biểu đồ tương tác khi không có link public: callback_data = 'chartfile:<MÃ>:<KHUNG>'."""
    query = update.callback_query
    _, symbol, *rest = query.data.split(":")
    await query.answer("Đang tạo biểu đồ tương tác…")
    await send_chart_file(context, query.message.chat.id, symbol, rest[0] if rest else DEFAULT_CHART_PERIOD)


# =====================================================================
# Backtest
# =====================================================================
BACKTEST_PERIODS = ("3M", "6M", "1Y")
VALIDATE_PERIODS = ("3M", "6M", "1Y")
PERIOD_TEXT = {"1M": "1 tháng", "3M": "3 tháng", "6M": "6 tháng", "1Y": "1 năm", "2Y": "2 năm"}
BACKTEST_KEYS = (" backtest ", " back test ", " kiem dinh chien luoc ", " kiem thu chien luoc ",
                 " test chien luoc ", " chay thu chien luoc ")
VALIDATE_KEYS = (" kiem dinh tin hieu ", " test qua khu ", " kiem tra qua khu ", " kiem dinh qua khu ",
                 " thu o qua khu ", " do chinh xac tin hieu ", " tin hieu co dung khong ", " validate ")


def is_backtest_request(text: str) -> bool:
    q = padded(text)
    return any(k in q for k in BACKTEST_KEYS)


def is_validation_request(text: str) -> bool:
    q = padded(text)
    return any(k in q for k in VALIDATE_KEYS)


def _bt_scope_token(symbols: list[str] | None) -> str:
    """Phần <phạm vi> trong callback_data '<bt|vd>:<phạm vi>:<khung>' (Telegram giới hạn 64 byte)."""
    if not symbols:
        return "*"
    token = ",".join(symbols)
    return token if len(token) <= 50 else "@last"


def _bt_scope_label(symbols: list[str] | None) -> str:
    return ", ".join(symbols) if symbols else f"{len(BACKTEST.symbols)} mã mặc định"


def _period_months(token: str | None, default: str) -> int:
    """'6M', '6T', '1Y', '1N', '2Y', '2N' -> số tháng."""
    raw = str(token or default).upper()
    raw = {"3T": "3M", "6T": "6M", "1N": "1Y", "12M": "1Y", "2N": "2Y", "24M": "2Y"}.get(raw, raw)
    return {"1M": 1, "3M": 3, "6M": 6, "1Y": 12, "2Y": 24}.get(raw, {"6M": 6, "1Y": 12}[default])


def _months_from_text(text: str, default: int) -> int:
    """'2 năm' -> 24, '1 năm' -> 12, '6 tháng' -> 6, 'tất cả' -> 24; không nhắc tới -> default."""
    q = padded(text)
    m = re.search(r" (\d{1,2}) ?(nam|n|y|year|years) ", q)
    if m:
        return min(24, 12 * int(m.group(1)))
    m = re.search(r" (\d{1,2}) ?(thang|t|m|month|months) ", q)
    if m:
        return min(24, max(1, int(m.group(1))))
    return 24 if any(k in q for k in (" tat ca ", " toan bo ")) else default


def research_menu_keyboard(symbols: list[str] | None) -> InlineKeyboardMarkup:
    """Hàng 1: backtest danh mục. Hàng 2: kiểm định tín hiệu quá khứ."""
    scope = _bt_scope_token(symbols)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🧪 Backtest {PERIOD_TEXT[p]}", callback_data=f"bt:{scope}:{p}") for p in BACKTEST_PERIODS],
        [InlineKeyboardButton(f"🔬 Độ chính xác {PERIOD_TEXT[p]}", callback_data=f"vd:{scope}:{p}") for p in VALIDATE_PERIODS],
    ])


def backtest_period_keyboard(symbols: list[str] | None, *, with_log: bool = False) -> InlineKeyboardMarkup:
    """Bàn phím dưới kết quả backtest: tải CSV, chạy lại khung khác, chuyển sang kiểm định tín hiệu."""
    scope = _bt_scope_token(symbols)
    rows = [[InlineKeyboardButton(f"{'🔁' if with_log else '🧪'} {PERIOD_TEXT[p]}",
                                  callback_data=f"bt:{scope}:{p}") for p in BACKTEST_PERIODS]]
    if with_log:
        rows.insert(0, [InlineKeyboardButton("📄 Trade Log + Nhật ký tín hiệu (CSV)", callback_data="btlog")])
        rows.append([InlineKeyboardButton("🔬 Kiểm định độ chính xác BUY/HOLD/SELL (6 tháng)",
                                          callback_data=f"vd:{scope}:6M")])
    return InlineKeyboardMarkup(rows)


def validation_keyboard(symbols: list[str] | None, horizon: int) -> InlineKeyboardMarkup:
    """Dưới kết quả kiểm định: tải CSV từng case, chấm lại ở tầm nhìn khác (tức thì), đổi giai đoạn."""
    scope = _bt_scope_token(symbols)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 Từng case: đúng/sai + nhận xét (CSV)", callback_data="vdlog")],
        [InlineKeyboardButton(f"{'• ' if h == horizon else ''}Chấm sau {h} phiên{' •' if h == horizon else ''}",
                              callback_data=f"vdh:{h}") for h in sv.HORIZONS],
        [InlineKeyboardButton(f"🔁 {PERIOD_TEXT[p]}", callback_data=f"vd:{scope}:{p}") for p in VALIDATE_PERIODS],
        [InlineKeyboardButton("🧪 Backtest danh mục 6 tháng", callback_data=f"bt:{scope}:6M")],
    ])


def _parse_scope(context, token: str) -> list[str] | None:
    if token == "*":
        return None
    if token == "@last":
        return context.chat_data.get("bt_last_symbols")
    return [s for s in token.upper().split(",") if s]


def start_backtest(context, chat_id: int, **kwargs) -> None:
    """Chạy backtest thành tác vụ nền: handler trả về ngay nên bot vẫn trả lời người khác trong lúc chờ."""
    context.application.create_task(run_backtest_flow(context, chat_id, **kwargs), name=f"backtest-{chat_id}")


def start_validation(context, chat_id: int, **kwargs) -> None:
    context.application.create_task(run_validation_flow(context, chat_id, **kwargs), name=f"validate-{chat_id}")


async def _run_job_with_progress(context, chat_id: int, title: str, scope: str, job):
    """Chạy `job(progress)` trong thread, cập nhật tiến độ trên một tin nhắn. Trả (kết quả, tin nhắn trạng thái);
    kết quả None nếu lỗi (đã báo người dùng)."""
    if bt.is_running():
        await context.bot.send_message(chat_id, "⏳ Đang có một backtest/kiểm định khác chạy (dùng chung quota "
                                                "VNStock). Thử lại sau ít phút nhé.")
        return None, None
    status = await context.bot.send_message(
        chat_id, ui.backtest_progress_text(scope, "data", 0, 1, "Chuẩn bị…", title=title), parse_mode=ParseMode.HTML)
    state: dict[str, str | None] = {"text": None}

    def progress(stage: str, done: int, total: int, label: str) -> None:
        # Chạy trong thread: chỉ ghi lại, vòng lặp async bên dưới mới sửa tin nhắn.
        state["text"] = ui.backtest_progress_text(scope, stage, done, total, label, title=title)

    task = asyncio.ensure_future(asyncio.to_thread(job, progress))
    shown = None
    while not task.done():
        await asyncio.wait({task}, timeout=3)
        if not task.done() and state["text"] and state["text"] != shown:
            try:
                await status.edit_text(state["text"], parse_mode=ParseMode.HTML)
                shown = state["text"]
            except TelegramError:
                pass   # "message is not modified" / giới hạn tần suất sửa tin: bỏ qua
    try:
        return task.result(), status
    except bt.BacktestBusy as exc:
        await status.edit_text(f"⏳ {exc}")
    except VnstockRateLimited:
        await status.edit_text(RATE_LIMIT_TEXT)
    except ValueError as exc:
        await status.edit_text(f"⚠️ Tham số không hợp lệ: {exc}")
    except Exception as exc:
        log.exception("%s %s", title, scope)
        await status.edit_text(f"Không chạy được lúc này.\n"
                               f"<code>{html.escape(type(exc).__name__)}: {html.escape(str(exc)[:250])}</code>",
                               parse_mode=ParseMode.HTML)
    return None, None


async def _delete(message) -> None:
    try:
        await message.delete()
    except TelegramError:
        pass


async def run_backtest_flow(context, chat_id: int, *, symbols: list[str] | None = None,
                            months: int | None = None, start=None, end=None) -> None:
    """Backtest danh mục: ảnh Equity Curve (hoặc dòng thời gian tín hiệu nếu 1 mã/0 lệnh) + thẻ kết quả."""
    scope = _bt_scope_label(symbols)
    report, status = await _run_job_with_progress(
        context, chat_id, "Đang backtest", scope,
        lambda progress: bt.run(symbols, start, end, months, progress=progress))
    if report is None:
        return
    context.chat_data["bt_last_report"] = report
    context.chat_data["bt_last_symbols"] = symbols
    m = report["metrics"]
    vni = "N/A" if m["vnindex_return"] is None else f"{m['vnindex_return']:+.2f}%"
    result = ("không có lệnh nào (giữ 100% tiền mặt)" if m["n_orders"] == 0 else
              f"<b>{m['total_return']:+.2f}%</b> · {m['n_trades']} giao dịch")
    caption = (f"🧪 <b>Backtest</b> {html.escape(scope)} · {report['start']:%d/%m/%Y}→{report['end']:%d/%m/%Y}\n"
               f"Danh mục: {result} · VN-Index {vni}")
    try:
        if len(report["symbols"]) == 1 or m["n_orders"] == 0:
            # Đường vốn phẳng không nói lên gì -> vẽ giá + điểm + ngưỡng BUY từng phiên của (các) mã.
            frame = bt.signal_log_frame(report)
            pngs = [await asyncio.to_thread(sv.render_timeline_png, frame, s, None,
                                            f"BACKTEST · {s} · điểm từng phiên so với ngưỡng BUY")
                    for s in report["symbols"][:3]]
        else:
            pngs = [await asyncio.to_thread(bt.render_report_png, report)]
    except Exception:
        log.exception("vẽ ảnh backtest")
        pngs = []
    await _delete(status)
    for k, png in enumerate(pngs):
        await context.bot.send_photo(chat_id, photo=png, caption=caption if k == 0 else None,
                                     parse_mode=ParseMode.HTML)
    await context.bot.send_message(chat_id, ui.backtest_card(report), parse_mode=ParseMode.HTML,
                                   reply_markup=backtest_period_keyboard(symbols, with_log=True))


async def _send_validation(context, chat_id: int, report: dict, symbols, cross) -> None:
    """Ảnh tổng hợp + ảnh từng mã (≤ 3) + thẻ kết quả."""
    try:
        pngs = [await asyncio.to_thread(sv.render_accuracy_summary_png, report)]
        if len(report["symbols"]) <= 3:
            for s in report["symbols"]:
                pngs.append(await asyncio.to_thread(sv.render_accuracy_png, report, s))
    except Exception:
        log.exception("vẽ ảnh kiểm định")
        pngs = []
    for png in pngs:
        await context.bot.send_photo(chat_id, photo=png)
    await context.bot.send_message(chat_id, ui.validation_card(report, cross), parse_mode=ParseMode.HTML,
                                   reply_markup=validation_keyboard(symbols, report["accuracy"]["h"]))


async def run_validation_flow(context, chat_id: int, *, symbols: list[str] | None = None,
                              months: int | None = None, start=None, end=None, horizon: int | None = None) -> None:
    """Kiểm định độ chính xác: chạy lại bot ở từng phiên quá khứ, chấm đúng/sai bằng giá tương lai."""
    scope = _bt_scope_label(symbols)
    report, status = await _run_job_with_progress(
        context, chat_id, "Đang kiểm định độ chính xác", scope,
        lambda progress: sv.run(symbols, start, end, None if start else (months or VALIDATION.months),
                                progress=progress, horizon=horizon))
    if report is None:
        return
    # Kiểm tra pipeline: phiên cuối của replay phải trùng với engine live (tối đa 3 mã để tiết kiệm quota).
    try:
        await status.edit_text(ui.backtest_progress_text(scope, "sim", 1, 1, "Đối chiếu với engine live…",
                                                         title="Đang kiểm định độ chính xác"),
                               parse_mode=ParseMode.HTML)
        cross = await asyncio.to_thread(sv.crosscheck_live, report, lambda s: analyze_symbol(s, None))
    except Exception:
        log.exception("crosscheck")
        cross = []
    context.chat_data["vd_last_report"] = report
    context.chat_data["vd_last_cross"] = cross
    context.chat_data["bt_last_symbols"] = symbols
    await _delete(status)
    await _send_validation(context, chat_id, report, symbols, cross)


async def validation_horizon_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Nút 'Chấm sau 5/10/20 phiên': chấm lại chính lần chạy trước, không tải lại dữ liệu."""
    query = update.callback_query
    report = context.chat_data.get("vd_last_report")
    if not report:
        await query.answer("Chưa có kết quả kiểm định (bot vừa khởi động lại?). Hãy chạy lại.", show_alert=True)
        return
    h = int(query.data.split(":", 1)[1])
    if h == report["accuracy"]["h"]:
        await query.answer(f"Đang xem chấm sau {h} phiên rồi.")
        return
    await query.answer(f"Chấm lại bằng giá {h} phiên sau…")
    report = await asyncio.to_thread(sv.rescore, report, h)
    context.chat_data["vd_last_report"] = report
    await _send_validation(context, query.message.chat_id, report, context.chat_data.get("bt_last_symbols"),
                           context.chat_data.get("vd_last_cross"))


def _parse_research_args(args) -> tuple[list[str], list, str | None]:
    """Tách tham số lệnh thành (mã, ngày, khung thời gian)."""
    raw_symbols, dates, period = [], [], None
    for arg in args:
        token = arg.upper()
        if token in {"1M", "3M", "6M", "1Y", "2Y", "3T", "6T", "1N", "2N", "12M", "24M", "ALL"}:
            period = "1Y" if token == "ALL" else token
            continue
        try:
            parsed = bt._parse_date(arg)
        except ValueError:
            parsed = None
        if parsed:
            dates.append(parsed)
        else:
            raw_symbols.append(arg)
    return raw_symbols, dates, period


async def validate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/kiemdinh [mã ...] [3M|6M|1Y|2Y] [5|10|20] [từ_ngày [đến_ngày]] — bot đúng bao nhiêu case trong quá khứ.

    VD: /kiemdinh FPT (6 tháng, chấm sau 20 phiên) · /kiemdinh FPT 1Y 10 · /kiemdinh 2026-01-01 2026-06-30
    """
    args = context.args or []
    if not args:
        await update.message.reply_text(ui.backtest_menu_text(), parse_mode=ParseMode.HTML,
                                        reply_markup=research_menu_keyboard(None))
        return
    raw_symbols, dates, period = _parse_research_args(args)
    horizon = None
    for raw in list(raw_symbols):
        token = raw.upper().rstrip("P")
        if token.isdigit() and int(token) in sv.HORIZONS:
            horizon = int(token)
            raw_symbols.remove(raw)
    symbols = []
    for raw in raw_symbols:
        symbol = await _resolve_arg(update, raw)
        if not symbol:
            return
        if symbol not in symbols:
            symbols.append(symbol)
    start_validation(context, update.effective_chat.id, symbols=symbols or None,
                     months=None if dates else _period_months(period, "6M"), horizon=horizon,
                     start=dates[0] if dates else None, end=dates[1] if len(dates) > 1 else None)


async def validation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Nút 🔬: callback_data = 'vd:<*|FPT,HPG|@last>:<6M|1Y|2Y>'."""
    query = update.callback_query
    _, token, period = (query.data.split(":") + ["6M"])[:3]
    symbols = _parse_scope(context, token)
    await query.answer(f"🔬 Kiểm định {_bt_scope_label(symbols)} · {PERIOD_TEXT.get(period, '')}")
    start_validation(context, query.message.chat_id, symbols=symbols, months=_period_months(period, "6M"))


async def validation_log_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    report = context.chat_data.get("vd_last_report")
    if not report:
        await query.answer("Chưa có kết quả kiểm định (bot vừa khởi động lại?). Hãy chạy lại.", show_alert=True)
        return
    await query.answer("Đang gửi file…")
    chat_id = query.message.chat_id
    stamp = f"{report['start']:%Y%m%d}_{report['end']:%Y%m%d}"
    h = report["accuracy"]["h"]
    await context.bot.send_document(
        chat_id, document=InputFile(io.BytesIO(sv.cases_csv(report)), filename=f"do_chinh_xac_{h}p_{stamp}.csv"),
        caption=(f"🔍 <b>Từng case</b>: ngày, mã, giá, kết luận của bot, điểm, thay đổi giá {h} phiên sau, "
                 "thực tế TĂNG/GIẢM, <b>ĐÚNG/SAI</b>, nhận xét, lý do của bot. Mở bằng Excel để lọc theo cột."),
        parse_mode=ParseMode.HTML)
    await context.bot.send_document(
        chat_id, document=InputFile(io.BytesIO(sv.trades_csv(report)), filename=f"kiem_dinh_lenh_ao_{stamp}.csv"),
        caption="🔁 Lệnh ảo khi làm đúng theo BUY→SELL của bot (mỗi mã độc lập, sau phí + thuế).")


async def backtest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/backtest [mã ...] [1M|3M|6M|1Y] [từ_ngày [đến_ngày]]. Không tham số -> menu chọn khung."""
    args = context.args or []
    if not args:
        await update.message.reply_text(ui.backtest_menu_text(), parse_mode=ParseMode.HTML,
                                        reply_markup=research_menu_keyboard(None))
        return
    symbols, dates, months = [], [], None
    for arg in args:
        period = charts.parse_period_token(arg)
        if period:
            months = bt.PERIOD_MONTHS.get(period, 12)   # ALL -> 1 năm
            continue
        try:
            parsed = bt._parse_date(arg)
        except ValueError:
            parsed = None
        if parsed:
            dates.append(parsed)
            continue
        symbol = await _resolve_arg(update, arg)
        if not symbol:
            return
        if symbol not in symbols:
            symbols.append(symbol)
    start = dates[0] if dates else None
    end = dates[1] if len(dates) > 1 else None
    start_backtest(context, update.effective_chat.id, symbols=symbols or None,
                   months=None if start else months, start=start, end=end)


async def backtest_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Nút “🧪 Backtest chiến lược”: callback_data = 'btmenu:<phạm vi>'."""
    query = update.callback_query
    await query.answer()
    symbols = _parse_scope(context, query.data.split(":", 1)[1])
    await query.message.reply_text(ui.backtest_menu_text(), parse_mode=ParseMode.HTML,
                                   reply_markup=research_menu_keyboard(symbols))


async def backtest_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Nút chạy backtest: callback_data = 'bt:<*|FPT,HPG|@last>:<3M|6M|1Y>'."""
    query = update.callback_query
    _, token, period = (query.data.split(":") + ["6M"])[:3]
    symbols = _parse_scope(context, token)
    months = bt.PERIOD_MONTHS.get(period, 6)
    await query.answer(f"🧪 Bắt đầu backtest {_bt_scope_label(symbols)} · {charts.PERIOD_LABELS.get(period, '')}")
    start_backtest(context, query.message.chat_id, symbols=symbols, months=months)


async def backtest_log_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Nút “📄 Trade Log”: gửi CSV của lần backtest gần nhất trong chat."""
    query = update.callback_query
    report = context.chat_data.get("bt_last_report")
    if not report:
        await query.answer("Chưa có kết quả backtest (bot vừa khởi động lại?). Hãy chạy lại.", show_alert=True)
        return
    await query.answer("Đang gửi Trade Log…")
    chat_id = query.message.chat_id
    stamp = f"{report['start']:%Y%m%d}_{report['end']:%Y%m%d}"
    n_orders = report["metrics"]["n_orders"]
    await context.bot.send_document(
        chat_id, document=InputFile(io.BytesIO(bt.signal_log_csv(report)), filename=f"backtest_nhat_ky_tin_hieu_{stamp}.csv"),
        caption=("🔍 <b>Nhật ký tín hiệu</b>: mỗi dòng = 1 phiên × 1 mã, gồm điểm T1/T2/T3, ngưỡng BUY, "
                 "6 điều kiện BUY (✓/✗), bộ lọc bị trượt và lý do. Dùng file này để kiểm tra vì sao từng ngày "
                 "có/không có BUY; dòng phiên cuối phải khớp với /signal cùng ngày."),
        parse_mode=ParseMode.HTML)
    await context.bot.send_document(
        chat_id, document=InputFile(io.BytesIO(bt.trade_log_csv(report)), filename=f"backtest_trade_log_{stamp}.csv"),
        caption=("📄 <b>Trade Log</b>: mã, ngày/giá tín hiệu, ngày/giá thực hiện (Open t+1), số lượng, giá trị, "
                 "phí, thuế, lãi/lỗ, lý do. Dòng <i>BỎ QUA</i> = BUY không khớp (giá trần, ngoài vùng mua…)."
                 if n_orders else "📄 <b>Trade Log</b>: trống vì không có lệnh nào được khớp trong kỳ."),
        parse_mode=ParseMode.HTML)
    await context.bot.send_document(
        chat_id, document=InputFile(io.BytesIO(bt.equity_csv(report)), filename=f"backtest_equity_{stamp}.csv"),
        caption="📈 Equity Curve theo ngày: tiền mặt, giá trị vị thế, tổng tài sản, VN-Index, drawdown.")


async def market_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Nút “🏛 Thị trường” trên màn hình chào."""
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(await asyncio.to_thread(ui.market_card), parse_mode=ParseMode.HTML)


# =====================================================================
# Chat tự nhiên
# =====================================================================
async def natural_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mọi tin nhắn không phải lệnh: chào hỏi, yêu cầu biểu đồ, hoặc câu hỏi cho copilot."""
    text = update.message.text.strip()
    chat = update.effective_chat
    history_before = list(_history(context))
    _remember(context, "user", text)

    if normalize_text(text) in GREETINGS:
        response = ("Xin chào 👋 Bạn có thể hỏi tự nhiên như “FPT hôm nay có nên mua không?”, "
                    "“FPT với VCB mã nào phù hợp hơn?”, “giá VNM bao nhiêu?”, “biểu đồ HPG” hoặc hỏi tiếp “tại sao?”.")
        await update.message.reply_text(response)
        _remember(context, "assistant", response)
        return

    if charts.is_chart_request(text):
        found = await asyncio.to_thread(resolve_symbols, text, 1)
        symbol = found[0] if found else context.chat_data.get("last_symbol")
        if not symbol:
            await update.message.reply_text("Bạn muốn xem biểu đồ mã nào? Ví dụ: “biểu đồ FPT” hoặc /chart FPT 3M.")
            return
        period = charts.period_from_text(text) or DEFAULT_CHART_PERIOD
        await send_chart(context, chat.id, chat.type, symbol, period)
        _remember(context, "assistant", f"[Đã gửi biểu đồ nến {symbol} {period}]")
        return

    if is_validation_request(text):
        found = await asyncio.to_thread(resolve_symbols, text, 8)
        months = _months_from_text(text, VALIDATION.months)
        m = re.search(r" (5|10|20) ?phien ", padded(text))
        start_validation(context, chat.id, symbols=found or None, months=months,
                         horizon=int(m.group(1)) if m else None)
        _remember(context, "assistant", f"[Đã chạy kiểm định tín hiệu {_bt_scope_label(found)} {months} tháng]")
        return

    if is_backtest_request(text):
        # "backtest 3 tháng", "backtest FPT HPG", "kiểm định chiến lược" -> chạy luôn; chỉ "backtest" -> menu.
        found = await asyncio.to_thread(resolve_symbols, text, 8)
        period = charts.period_from_text(text)
        if not found and not period:
            await update.message.reply_text(ui.backtest_menu_text(), parse_mode=ParseMode.HTML,
                                            reply_markup=research_menu_keyboard(None))
            _remember(context, "assistant", "[Đã gửi menu backtest]")
            return
        months = _months_from_text(text, 6)
        start_backtest(context, chat.id, symbols=found or None, months=months)
        _remember(context, "assistant", f"[Đã chạy backtest {_bt_scope_label(found)} {months} tháng]")
        return

    await context.bot.send_chat_action(chat.id, ChatAction.TYPING)
    reply = await asyncio.to_thread(
        run_financial_agent, text, history_before, context.chat_data.get("analysis_memory", {}),
        POSITIONS.get(chat.id, {}), context.chat_data.get("recent_symbols", []),
    )
    if reply.error:
        log.warning("agent chạy chế độ giảm cấp: %s | trace=%s", reply.error, reply.tool_trace)

    # Lưu kết quả engine (không phải câu chữ) để câu hỏi nối tiếp dựa trên số liệu thật; giữ 5 mã gần nhất.
    if reply.analyses:
        memory = context.chat_data.setdefault("analysis_memory", {})
        for symbol, result in reply.analyses.items():
            memory.pop(symbol, None)
            memory[symbol] = result
        while len(memory) > 5:
            memory.pop(next(iter(memory)))
    _remember_symbols(context, list(reply.analyses) or list(reply.quotes))

    if reply.mode == "comparison" and reply.analyses:
        rendered = ui.comparison_card(list(reply.analyses.values()), reply.text)
    elif reply.mode == "analysis" and reply.analyses:
        rendered = ui.conversational_stock_card(next(iter(reply.analyses.values())), reply.text)
    elif reply.mode == "quote" and reply.quotes:
        rendered = ui.multi_quote_card(reply.quotes, reply.text)
    elif reply.mode == "market" and reply.market:
        rendered = ui.market_snapshot_card(reply.market, reply.text)
    else:
        rendered = ui.agent_text_card(reply.text)

    markup = actions_keyboard(list(reply.analyses) or list(reply.quotes))
    if len(rendered) <= TELEGRAM_TEXT_LIMIT:
        await update.message.reply_text(rendered, parse_mode=ParseMode.HTML, reply_markup=markup)
    else:
        # Cắt ngang HTML có thể để thẻ mở dở -> gửi dạng chữ thường cho an toàn.
        plain = re.sub(r"<[^>]+>", "", rendered)[:TELEGRAM_TEXT_LIMIT - 50] + "\n…"
        await update.message.reply_text(plain, reply_markup=markup)
    _remember(context, "assistant", reply.text)


# =====================================================================
# Theo dõi định kỳ + khởi động
# =====================================================================
async def monitor_watchlist(context: ContextTypes.DEFAULT_TYPE):
    """Chạy mỗi ALERT_SECONDS: báo khi tín hiệu của mã trong watchlist thay đổi."""
    for chat_id, items in list(WATCHLIST.items()):
        for symbol, old_signal in list(items.items()):
            try:
                result = await asyncio.to_thread(analyze_symbol, symbol, position_for(chat_id, symbol))
                if old_signal is not None and result["signal"] != old_signal:
                    header = f"🔔 <b>TÍN HIỆU THAY ĐỔI</b>: {old_signal} → {result['signal']}\n\n"
                    await context.bot.send_message(chat_id, header + ui.signal_card(result), parse_mode=ParseMode.HTML,
                                                   reply_markup=actions_keyboard([symbol], detail=False))
                items[symbol] = result["signal"]
            except Exception as exc:
                log.warning("watch %s/%s lỗi: %s", chat_id, symbol, exc)
            await asyncio.sleep(0.5)   # giãn các lượt gọi, tránh dồn quota VNStock


BOT_COMMANDS = [
    ("signal", "Chấm điểm chi tiết một mã"), ("chart", "Biểu đồ nến"), ("backtest", "Backtest danh mục"),
    ("kiemdinh", "Độ chính xác BUY/HOLD/SELL trong quá khứ"),
    ("market", "Trạng thái thị trường"), ("top", "Xếp hạng các mã theo dõi"), ("quote", "Giá hiện tại"),
    ("watchlist", "Danh sách theo dõi"), ("positions", "Vị thế mô phỏng"), ("help", "Hướng dẫn"),
]


async def _post_init(app: Application) -> None:
    """Hiện danh sách lệnh (kể cả /backtest) trong nút Menu của Telegram."""
    try:
        await app.bot.set_my_commands([BotCommand(name, desc) for name, desc in BOT_COMMANDS])
    except TelegramError as exc:
        log.warning("Không đặt được menu lệnh: %s", exc)


def main():
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN chưa có trong .env")
        sys.exit(1)

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(_post_init).build()
    commands = {
        "start": start_cmd, "help": start_cmd, "signal": signal_cmd, "quote": quote_cmd, "chart": chart_cmd,
        "market": market_cmd, "top": top_cmd, "watch": watch_cmd, "unwatch": unwatch_cmd,
        "watchlist": watchlist_cmd, "position": position_cmd, "positions": positions_cmd,
        "backtest": backtest_cmd, "kiemdinh": validate_cmd, "validate": validate_cmd,
    }
    for name, handler in commands.items():
        app.add_handler(CommandHandler(name, handler))
    app.add_handler(CallbackQueryHandler(analyze_callback, pattern=r"^analyze:"))
    app.add_handler(CallbackQueryHandler(detail_callback, pattern=r"^detail:"))
    app.add_handler(CallbackQueryHandler(watch_callback, pattern=r"^watch:"))
    app.add_handler(CallbackQueryHandler(chart_callback, pattern=r"^chart:"))
    app.add_handler(CallbackQueryHandler(chart_file_callback, pattern=r"^chartfile:"))
    app.add_handler(CallbackQueryHandler(backtest_menu_callback, pattern=r"^btmenu:"))
    app.add_handler(CallbackQueryHandler(backtest_callback, pattern=r"^bt:"))
    app.add_handler(CallbackQueryHandler(backtest_log_callback, pattern=r"^btlog$"))
    app.add_handler(CallbackQueryHandler(validation_callback, pattern=r"^vd:"))
    app.add_handler(CallbackQueryHandler(validation_log_callback, pattern=r"^vdlog$"))
    app.add_handler(CallbackQueryHandler(validation_horizon_callback, pattern=r"^vdh:"))
    app.add_handler(CallbackQueryHandler(market_menu_callback, pattern=r"^menu:market$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, natural_message))
    if app.job_queue:
        app.job_queue.run_repeating(monitor_watchlist, interval=ALERT_SECONDS, first=ALERT_SECONDS)

    if DASHBOARD_ENABLED and start_dashboard_server(DASHBOARD_HOST, DASHBOARD_PORT):
        print(f"Dashboard: http://{DASHBOARD_HOST}:{DASHBOARD_PORT}/chart/FPT")
        print(f"Nút Telegram sẽ mở: {DASHBOARD_PUBLIC_URL}/chart/<MÃ>" if DASHBOARD_PUBLIC_URL else
              "Chưa đặt DASHBOARD_PUBLIC_URL -> nút 'Mở biểu đồ tương tác' sẽ gửi file HTML.")

    print("FinTech Bot đang chạy. Ctrl+C để dừng.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
