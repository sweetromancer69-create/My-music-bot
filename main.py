cat << 'EOF' > main.py
import os
import asyncio
import logging
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, FSInputFile
from aiogram.client.session.aiohttp import AiohttpSession
import yt_dlp

# Токен берем из переменных окружения (или подставьте свой, если переменная не задана)
BOT_TOKEN = os.getenv("BOT_TOKEN", "8881412253:AAELisPKS06kE8kIUG2kXZLfo-Jc8wHBMjk")
PROXY_URL = "http://proxy.server:3128"

logging.basicConfig(level=logging.INFO)

# КРИТИЧЕСКИ ВАЖНО для PythonAnywhere: передаем прокси в сессию aiogram, 
# чтобы обойти блокировку бесплатного аккаунта
session = AiohttpSession(proxy=PROXY_URL)
bot = Bot(token=BOT_TOKEN, session=session)
dp = Dispatcher()

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

def download_media(url: str):
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(DOWNLOAD_DIR, "%(title)s.%(ext)s"),
        "quiet": True,
        "noplaylist": True,
        "proxy": PROXY_URL,  # yt-dlp тоже должен качать через прокси PythonAnywhere!
        "nocheckcertificate": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)

        return {
            "file": filename,
            "title": info.get("title", "Unknown"),
            "uploader": info.get("uploader", "Unknown"),
        }

@dp.message(CommandStart())
async def start(message: Message):
    await message.answer("Привет! Отправь мне ссылку на трек, и я скачаю его.")

@dp.message(F.text)
async def download_handler(message: Message):
    url = message.text.strip()
    msg = await message.answer("⏳ Скачивание...")

    try:
        data = await asyncio.to_thread(download_media, url)

        await message.answer_audio(
            audio=FSInputFile(data["file"]),
            title=data["title"],
            performer=data["uploader"],
        )

        if os.path.exists(data["file"]):
            os.remove(data["file"])

        await msg.delete()

    except Exception as e:
        logging.exception(e)
        await msg.edit_text(f"❌ Ошибка:\n{e}")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
EOF
