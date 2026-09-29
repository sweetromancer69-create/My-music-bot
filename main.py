import asyncio
import logging
import os
import re
import shutil
from pathlib import Path

import yt_dlp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import FSInputFile, Message
from aiogram.client.session.aiohttp import AiohttpSession

# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "8881412253:AAELisPKS06kE8kIUG2kXZLfo-Jc8wHBMjk")
PROXY_URL = "http://proxy.server:3128"

# Если хочешь ограничить бота только своим Telegram ID:
ADMIN_ID = int(os.getenv("ADMIN_ID", "96349161"))

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Максимальное количество одновременных загрузок
MAX_CONCURRENT_DOWNLOADS = 2

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("music-bot")

# ============================================================
# BOT
# ============================================================

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN не установлен. "
        "Установи переменную окружения BOT_TOKEN."
    )

# Настройка сессии с прокси для PythonAnywhere
session = AiohttpSession(proxy=PROXY_URL)
bot = Bot(token=BOT_TOKEN, session=session)
dp = Dispatcher()

download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)


# ============================================================
# HELPERS
# ============================================================

def is_allowed(message: Message) -> bool:
    """
    Если ADMIN_ID=0, бот доступен всем.
    Если ADMIN_ID указан, только этому пользователю.
    """
    if ADMIN_ID == 0:
        return True

    return (
        message.from_user is not None
        and message.from_user.id == ADMIN_ID
    )


def safe_filename(name: str) -> str:
    """
    Удаляет символы, которые могут мешать созданию файла.
    """
    name = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", name)
    name = name.strip(" .")

    if not name:
        name = "audio"

    return name[:180]


def find_downloaded_audio(directory: Path) -> Path | None:
    """
    Ищет скачанный аудиофайл.
    """
    extensions = {
        ".mp3",
        ".m4a",
        ".opus",
        ".ogg",
        ".wav",
        ".flac",
        ".aac",
        ".webm",
    }

    files = [
        p
        for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in extensions
    ]

    if not files:
        return None

    return max(files, key=lambda p: p.stat().st_mtime)


def find_cover(directory: Path) -> Path | None:
    """
    Ищет обложку, если источник её предоставил.
    """
    extensions = {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
    }

    files = [
        p
        for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in extensions
    ]

    if not files:
        return None

    return max(files, key=lambda p: p.stat().st_mtime)


# ============================================================
# DOWNLOAD
# ============================================================

def download_audio(url: str, job_dir: Path) -> dict:
    """
    Скачивает аудио из источника через yt-dlp с поддержкой прокси.
    """
    output_template = str(job_dir / "%(title)s.%(ext)s")

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": output_template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "writethumbnail": True,
        "proxy": PROXY_URL,  # Прокси для yt-dlp
        "nocheckcertificate": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        
        audio_file = find_downloaded_audio(job_dir)
        cover_file = find_cover(job_dir)

        if not audio_file:
            raise FileNotFoundError("Аудиофайл не был найден после загрузки.")

        return {
            "file": audio_file,
            "cover": cover_file,
            "title": info.get("title", "Unknown Title"),
            "uploader": info.get("artist") or info.get("uploader", "Unknown Artist"),
        }


# ============================================================
# HANDLERS
# ============================================================

@dp.message(CommandStart())
async def start(message: Message):
    if not is_allowed(message):
        return
    await message.answer("Привет! Отправь мне ссылку на трек для скачивания.")


@dp.message(F.text)
async def download_handler(message: Message):
    if not is_allowed(message):
        return

    url = message.text.strip()
    if not url.startswith("http"):
        return

    msg = await message.answer("⏳ Скачивание и обработка...")
    
    # Создаем уникальную временную папку для задачи
    job_dir = DOWNLOAD_DIR / str(message.message_id)
    job_dir.mkdir(parents=True, exist_ok=True)

    async with download_semaphore:
        try:
            data = await asyncio.to_thread(download_audio, url, job_dir)

            await message.answer_audio(
                audio=FSInputFile(data["file"]),
                title=data["title"],
                performer=data["uploader"],
            )

            await msg.delete()

        except Exception as e:
            logger.exception(e)
            await msg.edit_text(f"❌ Ошибка:\n{e}")
        finally:
            # Очищаем временную папку задачи
            if job_dir.exists():
                shutil.rmtree(job_dir, ignore_errors=True)


# ============================================================
# MAIN
# ============================================================

async def main():
    logger.info("Бот запущен...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
