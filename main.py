import os
import asyncio
import logging
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.client.session.aiohttp import AiohttpSession
import yt_dlp

# Настройка логирования
logging.basicConfig(level=logging.INFO)

# Получаем переменные окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
PROXY_URL = os.getenv("PROXY_URL", "")

# Инициализация бота с поддержкой прокси (если он задан)
if PROXY_URL:
    session = AiohttpSession(proxy=PROXY_URL)
    bot = Bot(token=BOT_TOKEN, session=session)
else:
    bot = Bot(token=BOT_TOKEN)

dp = Dispatcher()

# Настройки для yt-dlp без лишних ограничений
ydl_opts = {
    'format': 'bestaudio/best',
    'postprocessors': [{
        'key': 'FFmpegExtractAudio',
        'preferredcodec': 'mp3',
        'preferredquality': '192',
    }],
    'outtmpl': '%(title)s.%(ext)s',
    'quiet': True,
}

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("Привет! Отправь мне ссылку на трек или видео, и я скачаю его для тебя.")

@dp.message(F.text)
async def handle_url(message: types.Message):
    url = message.text.strip()
    if not url.startswith("http"):
        await message.answer("Пожалуйста, отправь корректную ссылку.")
        return

    await message.answer("Скачиваю трек, подождите немного...")

    try:
        # Скачивание аудио через yt-dlp
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            # Меняем расширение на mp3, так как ffmpeg конвертирует его
            base, _ = os.path.splitext(filename)
            mp3_file = base + ".mp3"

        # Отправка файла пользователю
        audio_file = types.FSInputFile(mp3_file)
        await message.answer_audio(audio_file)

        # Удаление файла после отправки
        if os.path.exists(mp3_file):
            os.remove(mp3_file)

    except Exception as e:
        logging.error(f"Error downloading: {e}")
        await message.answer(f"Произошла ошибка при скачивании: {e}")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
