import os
import re
import json
import logging
import asyncio
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode, ChatAction
from telegram.error import TelegramError

from downloader import (
    extract_video_id,
    detect_platform,
    async_download_video,
    async_download_with_clearkey,
    NeedTokenError,
    build_kinescope_url,
    parse_kinescope_json,
    get_file_size_mb,
    cleanup_file,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "/tmp/video_downloads")
MAX_FILE_SIZE_MB = float(os.getenv("MAX_FILE_SIZE_MB", "2000"))
ALLOWED_USER_IDS_RAW = os.getenv("ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS = (
    set(int(uid.strip()) for uid in ALLOWED_USER_IDS_RAW.split(",") if uid.strip())
    if ALLOWED_USER_IDS_RAW
    else set()
)

PLATFORM_NAMES = {
    "youtube": "YouTube",
    "instagram": "Instagram",
    "vk": "VK Видео",
    "kinescope": "Kinescope",
}

# ── In-memory per-user state ─────────────────────────────────────────────────
# _json_pending[user_id] = parsed JSON info dict (waiting for download confirm)
_json_pending: dict[int, dict] = {}

# _token_pending[user_id] = parsed JSON info dict (waiting for ClearKey token)
_token_pending: dict[int, dict] = {}

# _url_pending[user_id] = {"url": str, "platform": str} (waiting for confirm)
_url_pending: dict[int, dict] = {}


def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    return user_id in ALLOWED_USER_IDS


# ── Commands ─────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_allowed(user.id):
        await update.message.reply_text("У вас нет доступа к этому боту.")
        return

    text = (
        f"Привет, {user.first_name}!\n\n"
        "Я скачиваю видео с популярных платформ.\n\n"
        "*Поддерживаемые сайты:*\n"
        "• YouTube (видео, Shorts, прямые эфиры)\n"
        "• Instagram (посты, Reels, IGTV)\n"
        "• VK Видео (vk.com/video, vkvideo.ru)\n"
        "• Kinescope (ссылки и JSON-файлы плеера)\n\n"
        "Просто отправь ссылку на видео!\n\n"
        "/help — подробная помощь"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("У вас нет доступа к этому боту.")
        return

    text = (
        "*Как использовать:*\n\n"
        "1. Отправь ссылку на видео\n"
        "2. Подтверди загрузку\n"
        "3. Получи видеофайл\n\n"
        "*Поддерживаемые платформы:*\n"
        "• *YouTube* — публичные видео, Shorts\n"
        "  `https://youtube.com/watch?v=...`\n"
        "  `https://youtu.be/...`\n\n"
        "• *Instagram* — публичные посты и Reels\n"
        "  `https://instagram.com/p/...`\n"
        "  `https://instagram.com/reel/...`\n\n"
        "• *VK Видео* — публичные видео\n"
        "  `https://vk.com/video...`\n"
        "  `https://vkvideo.ru/video...`\n\n"
        "• *Kinescope* — ссылка, JSON-файл или ID видео\n\n"
        f"*Максимальный размер файла:* {MAX_FILE_SIZE_MB:.0f} МБ\n\n"
        "⚠️ Приватные видео и видео с возрастными ограничениями могут не скачиваться."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


# ── Text message handler ──────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_allowed(user.id):
        await update.message.reply_text("У вас нет доступа к этому боту.")
        return

    text = update.message.text.strip()

    # ── ClearKey token reply ──────────────────────────────────────────────
    if user.id in _token_pending:
        await _handle_clearkey_token(update, context, text)
        return

    # ── Detect platform from URL ──────────────────────────────────────────
    platform = detect_platform(text)

    if platform in ("youtube", "instagram", "vk"):
        platform_name = PLATFORM_NAMES[platform]
        _url_pending[user.id] = {"url": text, "platform": platform}
        keyboard = [[
            InlineKeyboardButton("Скачать", callback_data="dl_url"),
            InlineKeyboardButton("Отмена", callback_data="cancel"),
        ]]
        await update.message.reply_text(
            f"*{platform_name}:*\n`{text}`\n\nЗагрузить?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    # ── Kinescope URL / ID ────────────────────────────────────────────────
    video_id = extract_video_id(text)
    if not video_id:
        if re.match(r"^[a-zA-Z0-9\-]{5,}$", text):
            video_id = text
        else:
            await update.message.reply_text(
                "Не могу найти видео.\n"
                "Отправьте ссылку с YouTube, Instagram, VK или Kinescope.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

    video_url = build_kinescope_url(video_id)
    keyboard = [[
        InlineKeyboardButton("Скачать", callback_data=f"dl:{video_id}"),
        InlineKeyboardButton("Отмена", callback_data="cancel"),
    ]]
    await update.message.reply_text(
        f"Найдено видео Kinescope:\n`{video_url}`\n\nЗагрузить?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def _handle_clearkey_token(
    update: Update, context: ContextTypes.DEFAULT_TYPE, token: str
) -> None:
    user = update.effective_user
    info = _token_pending.pop(user.id)

    await _run_download_drm(
        query_or_message=update.message,
        context=context,
        info=info,
        token=token,
    )


# ── Document (JSON) handler ───────────────────────────────────────────────────

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_allowed(user.id):
        await update.message.reply_text("У вас нет доступа к этому боту.")
        return

    doc = update.message.document
    if not (doc.file_name or "").lower().endswith(".json"):
        await update.message.reply_text("Пожалуйста, отправьте файл с расширением `.json`.")
        return

    if doc.file_size and doc.file_size > 10 * 1024 * 1024:
        await update.message.reply_text("Файл слишком большой (максимум 10 МБ).")
        return

    status = await update.message.reply_text("Читаю JSON…")

    try:
        tg_file = await context.bot.get_file(doc.file_id)
        raw = await tg_file.download_as_bytearray()
        data = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as e:
        await status.edit_text(f"Не удалось разобрать JSON: {e}")
        return
    except Exception as e:
        logger.error(f"Error reading JSON doc: {e}")
        await status.edit_text(f"Ошибка при чтении файла: {e}")
        return

    info = parse_kinescope_json(data)

    if not info.get("hls_url"):
        await status.edit_text(
            "Не нашёл HLS URL в JSON файле.\n"
            "Убедитесь, что это файл состояния плеера Kinescope."
        )
        return

    _json_pending[user.id] = info

    title = info.get("title") or info.get("video_id") or "видео"
    video_id_display = info.get("video_id") or "—"
    hls_short = info["hls_url"][:80] + "…"
    referrer = info.get("referrer") or "—"
    drm_note = (
        "\n⚠️ Видео защищено DRM (ClearKey). Бот попробует расшифровать автоматически."
        if info.get("clearkey_url")
        else ""
    )

    preview = (
        f"*Найдено в JSON:*\n"
        f"Название: `{title}`\n"
        f"ID: `{video_id_display}`\n"
        f"Referrer: `{referrer}`\n"
        f"HLS: `{hls_short}`"
        f"{drm_note}\n\nЗагрузить?"
    )

    keyboard = [[
        InlineKeyboardButton("Скачать", callback_data="dl_json"),
        InlineKeyboardButton("Отмена", callback_data="cancel"),
    ]]
    await status.edit_text(
        preview,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ── Callback handler ──────────────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = query.from_user

    if not is_allowed(user.id):
        await query.answer("У вас нет доступа.", show_alert=True)
        return

    await query.answer()

    if query.data == "cancel":
        _json_pending.pop(user.id, None)
        _token_pending.pop(user.id, None)
        _url_pending.pop(user.id, None)
        await query.edit_message_text("Отменено.")
        return

    if query.data == "dl_url":
        pending = _url_pending.pop(user.id, None)
        if not pending:
            await query.edit_message_text("Данные устарели. Отправьте ссылку ещё раз.")
            return
        platform_name = PLATFORM_NAMES.get(pending["platform"], pending["platform"])
        await _run_download(
            query=query,
            context=context,
            url=pending["url"],
            title=None,
            referrer=None,
            caption=f"{platform_name}: скачано ботом",
            platform=pending["platform"],
        )
        return

    if query.data.startswith("dl:"):
        video_id = query.data[3:]
        await _run_download(
            query=query,
            context=context,
            url=build_kinescope_url(video_id),
            title=None,
            referrer=None,
            caption=f"Kinescope: `{video_id}`",
            platform="kinescope",
        )
        return

    if query.data == "dl_json":
        info = _json_pending.pop(user.id, None)
        if not info:
            await query.edit_message_text(
                "Данные устарели. Пожалуйста, отправьте JSON ещё раз."
            )
            return

        if info.get("clearkey_url"):
            await _run_download_drm(
                query_or_message=query,
                context=context,
                info=info,
                token="",
            )
        else:
            await _run_download(
                query=query,
                context=context,
                url=info["hls_url"],
                title=info.get("title"),
                referrer=info.get("referrer"),
                caption=f"Kinescope: `{info.get('video_id') or info.get('title') or 'видео'}`",
                platform="kinescope",
            )


# ── Download helpers ──────────────────────────────────────────────────────────

async def _run_download(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    title: Optional[str],
    referrer: Optional[str],
    caption: str,
    platform: Optional[str] = None,
) -> None:
    chat_id = query.message.chat_id
    user_id = query.from_user.id

    status_msg = await query.edit_message_text(
        f"Начинаю загрузку…\n`{url[:80]}`",
        parse_mode=ParseMode.MARKDOWN,
    )

    user_dir = Path(DOWNLOAD_DIR) / str(user_id)
    user_dir.mkdir(parents=True, exist_ok=True)

    filepath = None
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)
        filepath = await async_download_video(
            url=url,
            output_dir=str(user_dir),
            progress_callback=_make_progress_callback(status_msg),
            referer=referrer,
            title=title,
            platform=platform,
        )
        await _send_video(context, chat_id, status_msg, filepath, caption)
    except Exception as e:
        await _handle_download_error(context, chat_id, status_msg, e, platform)
    finally:
        if filepath:
            cleanup_file(filepath)


async def _run_download_drm(
    query_or_message,  # CallbackQuery or Message
    context: ContextTypes.DEFAULT_TYPE,
    info: dict,
    token: str = "",
) -> None:
    from telegram import CallbackQuery
    if isinstance(query_or_message, CallbackQuery):
        chat_id = query_or_message.message.chat_id
        user_id = query_or_message.from_user.id
        status_msg = await query_or_message.edit_message_text(
            "Извлекаю ключи расшифровки из манифеста…"
        )
    else:
        chat_id = query_or_message.chat_id
        user_id = query_or_message.from_user.id
        status_msg = await query_or_message.reply_text(
            "Токен получен, извлекаю ключи расшифровки…"
        )

    user_dir = Path(DOWNLOAD_DIR) / str(user_id)
    user_dir.mkdir(parents=True, exist_ok=True)

    caption = f"Kinescope: `{info.get('video_id') or info.get('title') or 'видео'}`"
    filepath = None
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)
        filepath = await async_download_with_clearkey(
            hls_url=info["hls_url"],
            output_dir=str(user_dir),
            clearkey_url=info.get("clearkey_url", ""),
            token=token,
            referer=info.get("referrer"),
            title=info.get("title"),
            progress_callback=_make_progress_callback(status_msg),
        )
        await _send_video(context, chat_id, status_msg, filepath, caption)
    except NeedTokenError:
        _token_pending[user_id] = info
        await status_msg.edit_text(
            "*Автоматически получить токен не удалось.*\n\n"
            "Как найти токен вручную:\n"
            "1. Открой видео в браузере\n"
            "2. DevTools → Network → фильтр `license.kinescope.io`\n"
            "3. Скопируй значение `token=…` из URL запроса\n\n"
            "Отправь токен следующим сообщением (или /cancel для отмены):",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        await _handle_download_error(context, chat_id, status_msg, e, "kinescope")
    finally:
        if filepath:
            cleanup_file(filepath)


def _make_progress_callback(status_msg):
    last = {"pct": -10.0}

    async def cb(percent: float, downloaded: int, total: int) -> None:
        if percent - last["pct"] >= 10:
            last["pct"] = percent
            dl_mb = downloaded / (1024 * 1024)
            tot_mb = total / (1024 * 1024)
            bar = "█" * int(percent / 10) + "░" * (10 - int(percent / 10))
            try:
                await status_msg.edit_text(
                    f"Загружаю…\n`[{bar}]` {percent:.0f}%\n{dl_mb:.1f} / {tot_mb:.1f} МБ",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except TelegramError:
                pass

    return cb


async def _send_video(context, chat_id, status_msg, filepath, caption) -> None:
    if not filepath or not Path(filepath).exists():
        await status_msg.edit_text(
            "Не удалось скачать видео. Возможно, ссылка истекла или видео недоступно."
        )
        return

    size_mb = get_file_size_mb(filepath)
    if size_mb > MAX_FILE_SIZE_MB:
        await status_msg.edit_text(
            f"Видео слишком большое ({size_mb:.0f} МБ). Максимум: {MAX_FILE_SIZE_MB:.0f} МБ."
        )
        return

    await status_msg.edit_text(f"Скачано ({size_mb:.1f} МБ). Отправляю…")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)

    with open(filepath, "rb") as f:
        await context.bot.send_video(
            chat_id=chat_id,
            video=f,
            caption=caption,
            parse_mode=ParseMode.MARKDOWN,
            supports_streaming=True,
            read_timeout=300,
            write_timeout=300,
            connect_timeout=60,
        )
    await status_msg.delete()


async def _handle_download_error(
    context, chat_id, status_msg, exc: Exception, platform: Optional[str] = None
) -> None:
    logger.error(f"Download error: {exc}")
    err = str(exc)
    if "403" in err:
        msg = "Доступ запрещён (403). Ссылка могла истечь или видео недоступно."
    elif "404" in err:
        msg = "Видео не найдено (404)."
    elif "Private" in err or "private" in err.lower():
        msg = "Видео приватное — скачать невозможно."
    elif "unavailable" in err.lower():
        msg = "Видео недоступно в вашем регионе или было удалено."
    elif "age" in err.lower() or "sign in" in err.lower() or "login" in err.lower():
        msg = "Видео требует авторизации или имеет возрастное ограничение."
    elif "ключи" in err or "key" in err.lower() or "license" in err.lower():
        msg = f"Ошибка DRM: {err[:300]}"
    else:
        msg = f"Ошибка: {err[:300]}"
    try:
        await status_msg.edit_text(msg)
    except TelegramError:
        await context.bot.send_message(chat_id=chat_id, text=msg)


# ── Cancel command ────────────────────────────────────────────────────────────

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    _json_pending.pop(user.id, None)
    _token_pending.pop(user.id, None)
    _url_pending.pop(user.id, None)
    await update.message.reply_text("Отменено.")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN не установлен в .env файле")

    Path(DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Бот запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
