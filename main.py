import os
import asyncio
import logging
import urllib.parse
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

# Базовые настройки для yt-dlp (конвертация в MP3 + обход блокировок)
ydl_opts = {
    'format': 'bestaudio/best',
    'postprocessors': [{
        'key': 'FFmpegExtractAudio',
        'preferredcodec': 'mp3',
        'preferredquality': '192',
    }],
    'outtmpl': '%(id)s.%(ext)s',
    'quiet': True,
    # Эмуляция клиентов для обхода проверки "Sign in to confirm you're not a bot"
    'extractor_args': {
        'youtube': {
            'player_client': ['android', 'web'],
        }
    },
    'http_headers': {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
}

# Если в папке проекта есть файл cookies.txt, подключаем его
if os.path.exists("cookies.txt"):
    ydl_opts['cookiefile'] = 'cookies.txt'
    logging.info("Файл cookies.txt найден и успешно подключен к yt-dlp.")

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

        # 1. Если это прямая ссылка на YouTube или SoundCloud, качаем напрямую
        if "youtube.com" in url or "youtu.be" in url or "soundcloud.com" in url:
            search_target = url
        else:
            # 2. Для заблокированных сторонних сервисов (Beatport, Deezer) формируем поиск
            extracted_query = None

            # Пробуем вытащить метаданные страницы
            try:
                extract_opts = {'quiet': True, 'skip_download': True}
                if os.path.exists("cookies.txt"):
                    extract_opts['cookiefile'] = 'cookies.txt'

                with yt_dlp.YoutubeDL(extract_opts) as ydl:
                    info_meta = ydl.extract_info(url, download=False)
                    if info_meta:
                        title = info_meta.get('title')
                        uploader = info_meta.get('uploader') or info_meta.get('artist')
                        if title:
                            extracted_query = f"{uploader} - {title}" if uploader else title
            except Exception as meta_err:
                logging.info(f"Metadata extraction failed, falling back to URL parsing: {meta_err}")

            # Если метаданные недоступны, вырезаем название из URL-адреса (slug)
            if not extracted_query:
                parsed_url = urllib.parse.urlparse(url)
                path_parts = [p for p in parsed_url.path.split('/') if p]

                query_slug = ""
                for i, part in enumerate(path_parts):
                    if part in ['track', 'release', 'album'] and i + 1 < len(path_parts):
                        query_slug = path_parts[i + 1]
                        break

                if not query_slug and path_parts:
                    candidate = path_parts[-1]
                    if not candidate.isdigit():
                        query_slug = candidate

                clean_query = query_slug.replace('-', ' ').replace('_', ' ')
                if clean_query:
                    extracted_query = clean_query

            # Формируем итоговый поисковый запрос
            if extracted_query:
                search_target = f"ytsearch1:{extracted_query}"
                logging.info(f"Formed search query from URL: {search_target}")
            else:
                search_target = f"ytsearch1:{url}"

        # 3. Скачивание аудио через yt-dlp
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_target, download=True)

            if 'entries' in info:
                if not info['entries']:
                    raise Exception("По вашему запросу ничего не найдено.")
                info = info['entries'][0]

            filename = ydl.prepare_filename(info)
            base, _ = os.path.splitext(filename)
            mp3_file = base + ".mp3"

        # Проверяем созданный файл
        if not mp3_file or not os.path.exists(mp3_file):
            file_id = info.get('id')
            if file_id and os.path.exists(f"{file_id}.mp3"):
                mp3_file = f"{file_id}.mp3"
            else:
                raise Exception("Не удалось найти готовый MP3 файл после конвертации.")

        # 4. Отправка файла пользователю
        audio_file = types.FSInputFile(mp3_file)
        await message.answer_audio(audio_file)

    except Exception as e:
        logging.error(f"Error downloading: {e}")
        await message.answer(f"Произошла ошибка при скачивании: {e}")

    finally:
        if mp3_file and os.path.exists(mp3_file):
            try:
                os.remove(mp3_file)
            except Exception as cleanup_err:
                logging.error(f"Failed to remove temp file: {cleanup_err}")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
