import os
import re
import json
import logging
import asyncio
from pathlib import Path
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
    async_download_video,
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
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "/tmp/kinescope_downloads")
MAX_FILE_SIZE_MB = float(os.getenv("MAX_FILE_SIZE_MB", "2000"))
ALLOWED_USER_IDS_RAW = os.getenv("ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS = (
    set(int(uid.strip()) for uid in ALLOWED_USER_IDS_RAW.split(",") if uid.strip())
    if ALLOWED_USER_IDS_RAW
    else set()
)

# In-memory store for JSON-parsed video info keyed by user_id
# Avoids exceeding the 64-byte Telegram callback_data limit
_json_pending: dict[int, dict] = {}


def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    return user_id in ALLOWED_USER_IDS


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_allowed(user.id):
        await update.message.reply_text("У вас нет доступа к этому боту.")
        return

    text = (
        f"Привет, {user.first_name}!\n\n"
        "Я могу скачивать видео с Kinescope.\n\n"
        "Отправь мне:\n"
        "• Ссылку `https://kinescope.io/VIDEO_ID`\n"
        "• Ссылку `https://kinescope.io/embed/VIDEO_ID`\n"
        "• JSON-файл состояния плеера Kinescope\n"
        "• Или просто ID видео\n\n"
        "Команды:\n"
        "/start — это сообщение\n"
        "/help — помощь"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("У вас нет доступа к этому боту.")
        return

    text = (
        "*Как использовать бота:*\n\n"
        "1. Отправь ссылку на видео с Kinescope\n"
        "   или JSON-файл состояния плеера\n"
        "2. Бот скачает видео и пришлёт его тебе\n\n"
        "*Поддерживаемые форматы ссылок:*\n"
        "• `https://kinescope.io/VIDEO_ID`\n"
        "• `https://kinescope.io/embed/VIDEO_ID`\n"
        "• `https://player.kinescope.io/...`\n"
        "• Просто ID видео (буквы и цифры)\n\n"
        "*JSON-файл:* экспорт состояния плеера (содержит `url`, `referrer`, `options.playlist`)\n\n"
        f"*Ограничение:* файлы до {MAX_FILE_SIZE_MB:.0f} МБ"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_allowed(user.id):
        await update.message.reply_text("У вас нет доступа к этому боту.")
        return

    text = update.message.text.strip()

    video_id = extract_video_id(text)

    if not video_id:
        if re.match(r"^[a-zA-Z0-9\-]{5,}$", text):
            video_id = text
        else:
            await update.message.reply_text(
                "Не могу найти ID видео Kinescope в вашем сообщении.\n"
                "Отправьте ссылку вида: `https://kinescope.io/VIDEO_ID` "
                "или JSON-файл состояния плеера.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

    video_url = build_kinescope_url(video_id)

    keyboard = [
        [
            InlineKeyboardButton("Скачать", callback_data=f"dl:{video_id}"),
            InlineKeyboardButton("Отмена", callback_data="cancel"),
        ]
    ]
    await update.message.reply_text(
        f"Найдено видео Kinescope:\n`{video_url}`\n\nЗагрузить?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_allowed(user.id):
        await update.message.reply_text("У вас нет доступа к этому боту.")
        return

    doc = update.message.document
    filename = doc.file_name or ""

    if not filename.lower().endswith(".json"):
        await update.message.reply_text(
            "Пожалуйста, отправьте файл с расширением `.json`.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if doc.file_size and doc.file_size > 10 * 1024 * 1024:
        await update.message.reply_text("Файл слишком большой (максимум 10 МБ).")
        return

    status = await update.message.reply_text("Читаю JSON файл...")

    try:
        tg_file = await context.bot.get_file(doc.file_id)
        raw = await tg_file.download_as_bytearray()
        data = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as e:
        await status.edit_text(f"Не удалось разобрать JSON: {e}")
        return
    except Exception as e:
        logger.error(f"Error reading JSON document: {e}")
        await status.edit_text(f"Ошибка при чтении файла: {e}")
        return

    info = parse_kinescope_json(data)

    if not info.get("hls_url"):
        await status.edit_text(
            "Не нашёл HLS URL в JSON файле.\n"
            "Убедитесь, что это файл состояния плеера Kinescope "
            "(должен содержать `options.playlist[0].sources.hls.src`)."
        )
        return

    # Cache parsed info for this user so the callback can retrieve it
    _json_pending[user.id] = info

    title = info.get("title") or info.get("video_id") or "видео"
    video_id_display = info.get("video_id") or "—"
    hls_url = info["hls_url"]
    referrer = info.get("referrer") or "—"

    preview = (
        f"*Найдено в JSON:*\n"
        f"Название: `{title}`\n"
        f"ID: `{video_id_display}`\n"
        f"Referrer: `{referrer}`\n"
        f"HLS: `{hls_url[:80]}…`\n\n"
        f"Загрузить?"
    )

    keyboard = [
        [
            InlineKeyboardButton("Скачать", callback_data="dl_json"),
            InlineKeyboardButton("Отмена", callback_data="cancel"),
        ]
    ]
    await status.edit_text(
        preview,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = query.from_user

    if not is_allowed(user.id):
        await query.answer("У вас нет доступа.", show_alert=True)
        return

    await query.answer()

    if query.data == "cancel":
        await query.edit_message_text("Отменено.")
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
        )
        return

    if query.data == "dl_json":
        info = _json_pending.pop(user.id, None)
        if not info:
            await query.edit_message_text(
                "Данные устарели. Пожалуйста, отправьте JSON-файл ещё раз."
            )
            return
        await _run_download(
            query=query,
            context=context,
            url=info["hls_url"],
            title=info.get("title"),
            referrer=info.get("referrer"),
            caption=f"Kinescope: `{info.get('video_id') or info.get('title') or 'видео'}`",
        )


async def _run_download(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    title: str | None,
    referrer: str | None,
    caption: str,
) -> None:
    chat_id = query.message.chat_id
    user_id = query.from_user.id

    status_msg = await query.edit_message_text(
        f"Начинаю загрузку...\n`{url[:80]}`",
        parse_mode=ParseMode.MARKDOWN,
    )

    user_download_dir = Path(DOWNLOAD_DIR) / str(user_id)
    user_download_dir.mkdir(parents=True, exist_ok=True)

    last_update = {"percent": -10}

    async def progress_callback(percent: float, downloaded: int, total: int) -> None:
        if percent - last_update["percent"] >= 10:
            last_update["percent"] = percent
            downloaded_mb = downloaded / (1024 * 1024)
            total_mb = total / (1024 * 1024)
            bar = "█" * int(percent / 10) + "░" * (10 - int(percent / 10))
            try:
                await status_msg.edit_text(
                    f"Загружаю видео...\n"
                    f"`[{bar}]` {percent:.0f}%\n"
                    f"{downloaded_mb:.1f} / {total_mb:.1f} МБ",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except TelegramError:
                pass

    filepath = None
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)

        filepath = await async_download_video(
            url=url,
            output_dir=str(user_download_dir),
            progress_callback=progress_callback,
            referer=referrer,
            title=title,
        )

        if not filepath or not Path(filepath).exists():
            await status_msg.edit_text(
                "Не удалось скачать видео. Возможно, ссылка истекла или видео недоступно."
            )
            return

        file_size_mb = get_file_size_mb(filepath)

        if file_size_mb > MAX_FILE_SIZE_MB:
            await status_msg.edit_text(
                f"Видео слишком большое ({file_size_mb:.0f} МБ). "
                f"Максимум: {MAX_FILE_SIZE_MB:.0f} МБ."
            )
            return

        await status_msg.edit_text(f"Скачано ({file_size_mb:.1f} МБ). Отправляю...")
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)

        with open(filepath, "rb") as video_file:
            await context.bot.send_video(
                chat_id=chat_id,
                video=video_file,
                caption=caption,
                parse_mode=ParseMode.MARKDOWN,
                supports_streaming=True,
                read_timeout=300,
                write_timeout=300,
                connect_timeout=60,
            )

        await status_msg.delete()

    except Exception as e:
        logger.error(f"Download error ({url[:60]}): {e}")
        err = str(e)
        if "Private video" in err or "unavailable" in err:
            msg = "Видео приватное или недоступно."
        elif "403" in err:
            msg = "Доступ запрещён (403). Ссылка могла истечь — попробуйте получить новый JSON."
        elif "404" in err:
            msg = "Видео не найдено (404)."
        else:
            msg = f"Ошибка при загрузке: {err[:200]}"
        try:
            await status_msg.edit_text(msg)
        except TelegramError:
            await context.bot.send_message(chat_id=chat_id, text=msg)
    finally:
        if filepath:
            cleanup_file(filepath)


def main() -> None:
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN не установлен в .env файле")

    Path(DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Бот запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
