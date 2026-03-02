import os
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
        f"Привет, {user.first_name}! 👋\n\n"
        "Я могу скачивать видео с Kinescope.\n\n"
        "Просто отправь мне:\n"
        "• Ссылку на видео `https://kinescope.io/VIDEO_ID`\n"
        "• Ссылку на embed `https://kinescope.io/embed/VIDEO_ID`\n"
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
        "2. Бот скачает видео и пришлёт его тебе\n\n"
        "*Поддерживаемые форматы ссылок:*\n"
        "• `https://kinescope.io/VIDEO_ID`\n"
        "• `https://kinescope.io/embed/VIDEO_ID`\n"
        "• `https://player.kinescope.io/...`\n"
        "• Просто ID видео (буквы и цифры)\n\n"
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
        if re.match(r"^[a-zA-Z0-9]{5,}$", text):
            video_id = text
        else:
            await update.message.reply_text(
                "Не могу найти ID видео Kinescope в вашем сообщении.\n"
                "Пожалуйста, отправьте ссылку вида: `https://kinescope.io/VIDEO_ID`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

    video_url = build_kinescope_url(video_id)

    keyboard = [
        [
            InlineKeyboardButton("Скачать видео", callback_data=f"download:{video_id}"),
            InlineKeyboardButton("Отмена", callback_data="cancel"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"Найдено видео Kinescope:\n`{video_url}`\n\nЗагрузить?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup,
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

    if query.data.startswith("download:"):
        video_id = query.data.split(":", 1)[1]
        await process_download(query, context, video_id)


async def process_download(query, context: ContextTypes.DEFAULT_TYPE, video_id: str) -> None:
    chat_id = query.message.chat_id
    video_url = build_kinescope_url(video_id)

    status_msg = await query.edit_message_text(
        f"Начинаю загрузку...\n`{video_url}`",
        parse_mode=ParseMode.MARKDOWN,
    )

    user_download_dir = Path(DOWNLOAD_DIR) / str(query.from_user.id)
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
            video_url,
            str(user_download_dir),
            progress_callback=progress_callback,
        )

        if not filepath or not Path(filepath).exists():
            await status_msg.edit_text(
                "Не удалось скачать видео. Возможно, видео закрыто или недоступно."
            )
            return

        file_size_mb = get_file_size_mb(filepath)

        if file_size_mb > MAX_FILE_SIZE_MB:
            await status_msg.edit_text(
                f"Видео слишком большое ({file_size_mb:.0f} МБ). "
                f"Максимум: {MAX_FILE_SIZE_MB:.0f} МБ."
            )
            return

        await status_msg.edit_text(
            f"Видео скачано ({file_size_mb:.1f} МБ). Отправляю..."
        )

        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO)

        with open(filepath, "rb") as video_file:
            await context.bot.send_video(
                chat_id=chat_id,
                video=video_file,
                caption=f"Kinescope: `{video_id}`",
                parse_mode=ParseMode.MARKDOWN,
                supports_streaming=True,
                read_timeout=300,
                write_timeout=300,
                connect_timeout=60,
            )

        await status_msg.delete()

    except Exception as e:
        logger.error(f"Error processing download for {video_id}: {e}")
        error_text = str(e)
        if "Private video" in error_text or "This video is unavailable" in error_text:
            msg = "Видео приватное или недоступно."
        elif "HTTP Error 403" in error_text:
            msg = "Доступ запрещён (403). Возможно, требуется авторизация."
        elif "HTTP Error 404" in error_text:
            msg = "Видео не найдено (404)."
        else:
            msg = f"Ошибка при загрузке: {error_text[:200]}"
        try:
            await status_msg.edit_text(msg)
        except TelegramError:
            await context.bot.send_message(chat_id=chat_id, text=msg)
    finally:
        if filepath:
            cleanup_file(filepath)


import re


def main() -> None:
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN не установлен в .env файле")

    Path(DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Бот запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
