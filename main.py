import os
import asyncio
import logging
import tempfile
from pathlib import Path
import requests
from bs4 import BeautifulSoup
import yt_dlp
from mutagen.id3 import ID3, TIT2, TPE1
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command

logging.basicConfig(level=logging.INFO)

# ==================== НАСТРОЙКИ ====================
BOT_TOKEN = "8881412253:AAELisPKS06kE8kIUG2kXZLfo-Jc8wHBMjk"
ADMIN_ID = 96349161
# ===================================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

def get_universal_metadata(url: str):
    """
    Универсальный парсер: сначала пробует извлечь метаданные через yt-dlp,
    а если сервис закрыт (как Qobuz/Beatport), подключает BeautifulSoup по og:title.
    """
    # 1. Попытка через yt-dlp (отлично работает для Apple Music, Deezer, Amazon, Tidal)
    try:
        ydl_opts = {"extract_flat": True, "quiet": True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info:
                title = info.get("title")
                uploader = info.get("uploader") or info.get("artist")
                if title:
                    return title, uploader or ""
    except Exception:
        pass

    # 2. Универсальный резервный вариант через requests + BeautifulSoup (для всех остальных)
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            
            # Проверяем стандартные теги заголовков
            for prop in ["og:title", "twitter:title"]:
                tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
                if tag and tag.get("content"):
                    full_text = tag["content"]
                    # Очищаем от мусора популярных платформ
                    for suffix in [
                        " | Qobuz", " on Qobuz", " on Beatport", 
                        " - Apple Music", " - song and lyrics by", 
                        " | Deezer", " | Tidal", " | Amazon Music"
                    ]:
                        if suffix in full_text:
                            full_text = full_text.split(suffix)[0]
                    return full_text, ""
            
            # Если тегов нет, берем тег <title> страницы
            if soup.title and soup.title.string:
                return soup.title.string.strip(), ""
                
    except Exception as e:
        logging.error(f"Ошибка универсального парсинга: {e}")

    return None, None

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if ADMIN_ID and message.from_user.id != int(ADMIN_ID):
        await message.answer("У вас нет доступа к этому боту.")
        return

    await message.answer(
        "🎧 <b>Мульти-бот активен!</b>\n\n"
        "Отправь мне ссылку на трек с любой поддерживаемой платформы:\n"
        "• Beatport\n• Qobuz\n• Apple Music\n• Deezer\n• Tidal\n• Amazon Music\n\n"
        "Я найду аудио, оформлю теги и пришлю MP3 в высоком качестве.",
        parse_mode="HTML"
    )

@dp.message(F.text)
async def handle_music_link(message: types.Message):
    if ADMIN_ID and message.from_user.id != int(ADMIN_ID):
        return

    url = message.text.strip()

    if not url.startswith(("http://", "https://")):
        await message.answer("Пожалуйста, отправь корректную ссылку.")
        return

    status_msg = await message.answer("🔍 Читаю ссылку и распознаю трек...")

    temp_dir = tempfile.mkdtemp(prefix="music_bot_")
    try:
        # 1. Извлекаем название и артиста
        track_title, track_artist = get_universal_metadata(url)

        if not track_title:
            await status_msg.edit_text("Не удалось распознать трек по этой ссылке.")
            return

        search_query = f"{track_artist} - {track_title}" if track_artist else track_title
        await status_msg.edit_text(f"🎵 Найдено: <b>{search_query}</b>\n⏳ Ищу и скачиваю аудио...", parse_mode="HTML")

        # 2. Скачиваем файл через поисковой механизм с обходом блокировок
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": os.path.join(temp_dir, "%(id)s.%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-us,en;q=0.5",
            },
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "320",
                }
            ],
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            search_target = f"ytsearch1:{search_query}"
            info = ydl.extract_info(search_target, download=True)
            
            if "entries" in info:
                info = info["entries"][0]

            downloaded_file = Path(ydl.prepare_filename(info)).with_suffix(".mp3")

        if not downloaded_file.exists():
            candidates = list(Path(temp_dir).glob("*.mp3"))
            if not candidates:
                raise RuntimeError("Не удалось сохранить аудио файл.")
            downloaded_file = candidates[0]

        # 3. Записываем ID3-теги через mutagen
        try:
            audio = ID3(str(downloaded_file))
        except Exception:
            audio = ID3()
            
        if track_title:
            audio["TIT2"] = TIT2(encoding=3, text=track_title)
        if track_artist:
            audio["TPE1"] = TPE1(encoding=3, text=track_artist)
        audio.save(str(downloaded_file))

        # 4. Отправляем готовый трек в Telegram
        audio_input = types.FSInputFile(str(downloaded_file))
        await message.answer_audio(
            audio_input,
            title=track_title,
            performer=track_artist or "Music Release"
        )
        await status_msg.delete()

    except Exception as e:
        logging.exception("Error processing link")
        await status_msg.edit_text(f"Произошла ошибка при обработке: {str(e)[:300]}")

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
