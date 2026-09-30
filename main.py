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

# ==================== НАСТРОЙКИ ИЗ ОКРУЖЕНИЯ ====================
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ.get("ADMIN_ID", "963491961"))
# ===============================================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

def get_universal_metadata(url: str):
    """
    Универсальный парсер: сначала пробует yt-dlp, затем BeautifulSoup для извлечения названия и автора.
    """
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

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            
            for prop in ["og:title", "twitter:title"]:
                tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
                if tag and tag.get("content"):
                    full_text = tag["content"]
                    for suffix in [
                        " | Qobuz", " on Qobuz", " on Beatport", 
                        " - Apple Music", " - song and lyrics by", 
                        " | Deezer", " | Tidal", " | Amazon Music", " | SoundCloud"
                    ]:
                        if suffix in full_text:
                            full_text = full_text.split(suffix)[0]
                    return full_text, ""
            
            if soup.title and soup.title.string:
                return soup.title.string.strip(), ""
                
    except Exception as e:
        logging.error(f"Ошибка универсального парсинга: {e}")

    return None, None

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    print(f"DEBUG_LOG ---> Команда /start от User ID: {message.from_user.id}")
    if ADMIN_ID and message.from_user.id != ADMIN_ID:
        print(f"DEBUG_LOG ---> Доступ запрещен для ID {message.from_user.id}")
        await message.answer("У вас нет доступа к этому боту.")
        return

    await message.answer(
        "🎧 <b>Мульти-бот запущен!</b>\n\n"
        "Отправь мне ссылку на трек (Beatport, SoundCloud, Qobuz, Apple Music и др.):\n\n"
        "Я найду аудио на альтернативных площадках, оформлю теги и пришлю MP3 (320kbps).",
        parse_mode="HTML"
    )

@dp.message(F.text)
async def handle_music_link(message: types.Message):
    print(f"DEBUG_LOG ---> Получено сообщение от User ID: {message.from_user.id} | Текст: {message.text}")

    if ADMIN_ID and message.from_user.id != ADMIN_ID:
        print(f"DEBUG_LOG ---> Доступ отклонен! Ожидался ADMIN_ID={ADMIN_ID}, а пришел {message.from_user.id}")
        return

    url = message.text.strip()
    if not url.startswith(("http://", "https://")):
        await message.answer("Пожалуйста, отправь корректную ссылку.")
        return

    status_msg = await message.answer("🔍 Читаю ссылку и распознаю трек...")
    print("DEBUG_LOG ---> Отправлен статус: Читаю ссылку...")

    temp_dir = tempfile.mkdtemp(prefix="music_bot_")
    try:
        track_title, track_artist = get_universal_metadata(url)
        print(f"DEBUG_LOG ---> Распознано: Артист='{track_artist}' | Трек='{track_title}'")

        if not track_title:
            track_title = url

        search_query = f"{track_artist} - {track_title}" if track_artist else track_title
        await status_msg.edit_text(f"🎵 Найдено: <b>{search_query}</b>\n⏳ Ищу и скачиваю аудио...", parse_mode="HTML")

        # Используем безопасно короткое имя файла на диске по ID, чтобы избегать длинных путей
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": os.path.join(temp_dir, "%(id)s.%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            },
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "320",
                }
            ],
        }

        # Каскадный поиск по альтернативным каталогам (без YouTube)
        search_providers = [
            f"scsearch1:{search_query}",       # 1. SoundCloud
            f"bandcampsearch1:{search_query}", # 2. Bandcamp
            f"vksearch1:{search_query}"        # 3. VK
        ]

        print(f"DEBUG_LOG ---> Запуск каскадного поиска для: {search_query}")
        
        info = None
        downloaded_file = None

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            for search_target in search_providers:
                try:
                    print(f"DEBUG_LOG ---> Пробую найти через: {search_target}")
                    res = ydl.extract_info(search_target, download=True)
                    
                    if res:
                        if "entries" in res and len(res["entries"]) > 0:
                            info = res["entries"][0]
                        else:
                            info = res
                        
                        if info:
                            filename = ydl.prepare_filename(info)
                            downloaded_file = Path(filename).with_suffix(".mp3")
                            if downloaded_file.exists():
                                print(f"DEBUG_LOG ---> Успешно найдено и скачано через {search_target}")
                                if not track_title or track_title == url:
                                    track_title = info.get("title", "Unknown Track")
                                if not track_artist:
                                    track_artist = info.get("uploader", "Musicvibez")
                                break
                except Exception as e:
                    print(f"DEBUG_LOG ---> Платформа {search_target} не дала результатов: {e}")
                    continue

        if not downloaded_file or not downloaded_file.exists():
            candidates = list(Path(temp_dir).glob("*.mp3"))
            if not candidates:
                raise RuntimeError("Трек не найден ни на одной из альтернативных платформ.")
            downloaded_file = candidates[0]

        print(f"DEBUG_LOG ---> Записываем теги: Артист='{track_artist}', Трек='{track_title}'")
        try:
            audio = ID3(str(downloaded_file))
        except Exception:
            audio = ID3()
            
        audio["TIT2"] = TIT2(encoding=3, text=track_title or "Unknown Track")
        audio["TPE1"] = TPE1(encoding=3, text=track_artist or "Musicvibez")
        audio.save(str(downloaded_file))

        print(f"DEBUG_LOG ---> Отправка аудиофайла в Telegram...")
        audio_input = types.FSInputFile(str(downloaded_file))
        await message.answer_audio(
            audio_input,
            title=track_title or "Track",
            performer=track_artist or "Musicvibez.org"
        )
        await status_msg.delete()
        print(f"DEBUG_LOG ---> Трек успешно отправлен!")

    except Exception as e:
        logging.exception("Error processing link")
        print(f"DEBUG_LOG ---> ОШИБКА: {str(e)}")
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
