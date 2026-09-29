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

# Базовые настройки для yt-dlp (конвертация в MP3)
ydl_opts = {
    'format': 'bestaudio/best',
    'postprocessors': [{
        'key': 'FFmpegExtractAudio',
        'preferredcodec': 'mp3',
        'preferredquality': '192',
    }],
    'outtmpl': '%(id)s.%(ext)s',
    'quiet': True,
}

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("Привет! Отправь мне ссылку на трек (Beatport, Deezer, YouTube и др.), и я найду и скачаю его для тебя в MP3.")

@dp.message(F.text)
async def handle_url(message: types.Message):
    url = message.text.strip()
    if not url.startswith("http"):
        await message.answer("Пожалуйста, отправь корректную ссылку.")
        return

    await message.answer("Ищу и скачиваю трек, подождите немного...")

    mp3_file = None
    try:
        search_target = url

        # Шаг 1: Если это Beatport, Deezer или другой защищенный сайт,
        # пробуем вытащить название трека через метаданные страницы без скачивания
        extract_opts = {'quiet': True, 'skip_download': True}
        try:
            with yt_dlp.YoutubeDL(extract_opts) as ydl:
                info_meta = ydl.extract_info(url, download=False)
                if info_meta:
                    title = info_meta.get('title')
                    uploader = info_meta.get('uploader') or info_meta.get('artist')
                    if title:
                        # Формируем поисковый запрос по названию и артисту
                        search_target = f"ytsearch1:{uploader} - {title}" if uploader else f"ytsearch1:{title}"
                        logging.info(f"Resolved URL to search query: {search_target}")
        except Exception as meta_err:
            logging.warning(f"Could not extract metadata directly, falling back to direct URL or search: {meta_err}")
            # Если не получилось вытащить метаданные напрямую, пробуем искать саму ссылку через ytsearch
            search_target = f"ytsearch1:{url}"

        # Шаг 2: Скачиваем аудио через yt-dlp (по найденному названию или прямой ссылке)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_target, download=True)
            
            # Если это был поиск (ytsearch), yt-dlp возвращает список в ключе 'entries'
            if 'entries' in info:
                if not info['entries']:
                    raise Exception("По вашему запросу ничего не найдено.")
                info = info['entries'][0]

            filename = ydl.prepare_filename(info)
            base, _ = os.path.splitext(filename)
            mp3_file = base + ".mp3"

        # Проверяем, создался ли файл
        if not mp3_file or not os.path.exists(mp3_file):
            # Попробуем найти файл по id если стандартный путь отличается
            file_id = info.get('id')
            if file_id and os.path.exists(f"{file_id}.mp3"):
                mp3_file = f"{file_id}.mp3"
            else:
                raise Exception("Не удалось найти готовый MP3 файл после конвертации.")

        # Шаг 3: Отправка файла пользователю
        audio_file = types.FSInputFile(mp3_file)
        await message.answer_audio(audio_file)

    except Exception as e:
        logging.error(f"Error downloading: {e}")
        await message.answer(f"Произошла ошибка при скачивании: {e}")

    finally:
        # Очистка файла после отправки
        if mp3_file and os.path.exists(mp3_file):
            try:
                os.remove(mp3_file)
            except Exception as cleanup_err:
                logging.error(f"Failed to remove temp file: {cleanup_err}")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
