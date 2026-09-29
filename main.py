import os
import asyncio
import logging
import json
import urllib.request
import urllib.parse
from pathlib import Path
from bs4 import BeautifulSoup

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.client.session.aiohttp import AiohttpSession
import yt_dlp

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
PROXY_URL = os.getenv("PROXY_URL", "")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not configured")

if PROXY_URL:
    session = AiohttpSession(proxy=PROXY_URL)
    bot = Bot(token=BOT_TOKEN, session=session)
else:
    bot = Bot(token=BOT_TOKEN)

dp = Dispatcher()

def get_ydl_options(output_dir):
    return {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(output_dir, "%(title)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
    }

def parse_beatport_metadata(url: str) -> dict:
    """Пытается вытащить метаданные (жанр, дату и т.д.) с публичной страницы Beatport"""
    meta_info = {
        "genre": "Не указан",
        "bpm": "Не указан",
        "key": "Не указан",
        "date": "Не указана"
    }
    try:
        req = urllib.request.Request(
            url, 
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            html = response.read().decode('utf-8')
            soup = BeautifulSoup(html, 'html.parser')
            
            # Ищем скрытый JSON со структурированными данными о треке/релизе
            json_ld = soup.find('script', type='application/ld+json')
            if json_ld:
                data = json.loads(json_ld.string)
                if isinstance(data, list):
                    data = data[0]
                if 'datePublished' in data:
                    meta_info['date'] = data['datePublished'][:10]
            
            # Парсим текстовые блоки на странице Beatport (жанры, бпм часто идут в тегах)
            # Примерный поиск элементов с характеристиками на странице
            for div in soup.find_all('div', class_=lambda x: x and ('bucket' in x or 'metadata' in x)):
                text = div.get_text()
                if "BPM" in text:
                    # Можно выцепить BPM регуляркой или просто текстом
                    pass
    except Exception as e:
        logging.warning(f"Не удалось распарсить метаданные страницы: {e}")
        
    return meta_info

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "Привет! Отправь ссылку на трек (YouTube, SoundCloud, Beatport и др.), "
        "и я постараюсь найти и скачать его для тебя вместе с метаданными."
    )

@dp.message(F.text)
async def handle_url(message: types.Message):
    url = message.text.strip()

    if not url.startswith(("http://", "https://")):
        await message.answer("Отправь корректную ссылку.")
        return

    status_msg = await message.answer("🔍 Ищу трек и анализирую ссылку...")

    temp_dir = tempfile.mkdtemp(prefix="musicbot_") if 'tempfile' in globals() else None
    # Если tempfile импортирован через стандартный модуль в вашем коде, используем его:
    import tempfile
    temp_dir = tempfile.mkdtemp(prefix="musicbot_")
    
    mp3_file = None
    beatport_meta = {}

    try:
        # Если это Beatport, пробуем сразу собрать метаданные для красивого отчета
        if "beatport.com" in url:
            beatport_meta = parse_beatport_metadata(url)
            
        # Формируем поисковой запрос: если это Beatport, пытаемся вытащить имя для поиска в yt-dlp
        search_target = url
        if "beatport.com" in url:
            # Выдергиваем slug из урла для поиска на ютубе, если прямая ссылка не скачается
            parsed = urllib.parse.urlparse(url)
            path_parts = [p for p in parsed.path.split('/') if p]
            if path_parts:
                query_candidate = path_parts[-1].replace('-', ' ').replace('_', ' ')
                if not query_candidate.isdigit():
                    search_target = f"ytsearch1:{query_candidate}"

        with yt_dlp.YoutubeDL(get_ydl_options(temp_dir)) as ydl:
            info = ydl.extract_info(search_target, download=True)

            if not info:
                raise RuntimeError("Не удалось получить информацию об аудио.")

            if "entries" in info:
                entries = info["entries"]
                if not entries:
                    raise RuntimeError("Аудио не найдено.")
                info = entries[0]

            downloaded = Path(ydl.prepare_filename(info))
            mp3_file = downloaded.with_suffix(".mp3")

        if not mp3_file.exists():
            candidates = list(Path(temp_dir).glob("*.mp3"))
            if not candidates:
                raise RuntimeError("Источник не предоставил доступный аудиофайл.")
            mp3_file = candidates[0]

        audio_file = types.FSInputFile(str(mp3_file))
        
        title = info.get("title", "Unknown Title")
        performer = info.get("artist") or info.get("uploader", "Unknown Artist")

        # Собираем красивую карточку в стиле диджейских ботов
        caption_lines = [
            f"🎵 **{performer} — {title}**",
            f"━━━━━━━━━━━━━━━━━━"
        ]
        
        if "beatport.com" in url:
            if beatport_meta.get("genre") and beatport_meta.get("genre") != "Не указан":
                caption_lines.append(f"🏷 Жанр: {beatport_meta['genre']}")
            if beatport_meta.get("date") and beatport_meta.get("date") != "Не указана":
                caption_lines.append(f"📅 Дата релиза: {beatport_meta['date']}")
            caption_lines.append(f"🤖 Источник: Beatport (через поиск)")
        else:
            caption_lines.append(f"🔗 Платформа: YouTube / SoundCloud")

        caption = "\n".join(caption_lines)

        await message.answer_audio(
            audio_file,
            caption=caption,
            parse_mode="Markdown",
            title=title,
            performer=performer,
        )
        await bot.delete_message(chat_id=message.chat.id, message_id=status_msg.message_id)

    except Exception as e:
        logging.exception("Download error")
        await message.answer(f"Не удалось скачать аудио.\n\n{str(e)[:700]}")
        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=status_msg.message_id)
        except:
            pass

    finally:
        try:
            for file in Path(temp_dir).glob("*"):
                file.unlink(missing_ok=True)
            Path(temp_dir).rmdir()
        except Exception:
            pass

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
