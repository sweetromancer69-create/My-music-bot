import os
import asyncio
import logging
import tempfile
import json
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
    Продвинутый парсер для Beatport и других платформ: извлекает чистые метаданные.
    """
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            
            # Проверяем JSON-данные Beatport (__NEXT_DATA__)
            next_data = soup.find("script", id="__NEXT_DATA__")
            if next_data:
                try:
                    data = json.loads(next_data.string)
                    # Пытаемся вытащить данные трека из кэша Apollo / структуры страницы Beatport
                    queries = data.get("props", {}).get("pageProps", {}).get("dehydratedState", {}).get("queries", [])
                    for q in queries:
                        state_data = q.get("state", {}).get("data", {})
                        if "track" in state_data:
                            track_info = state_data["track"]
                            title = track_info.get("name")
                            mix = track_info.get("mix_name")
                            if mix and mix != "Original Mix":
                                title = f"{title} ({mix})"
                            artists = ", ".join([a["name"] for a in track_info.get("artists", [])])
                            if title and artists:
                                return title, artists
                except Exception:
                    pass

            # Запасной вариант через Open Graph теги
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
                    
                    # Если формат "Трек - Артист" или наоборот
                    parts = full_text.split(" - ")
                    if len(parts) >= 2:
                        return parts[0].strip(), parts[1].strip()
                    return full_text.strip(), ""
            
            if soup.title and soup.title.string:
                return soup.title.string.strip(), ""
                
    except Exception as e:
        logging.error(f"Ошибка парсинга метаданных: {e}")

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
        "Я найду аудио на альтернативных площадках (без YouTube), оформлю теги и пришлю MP3 (320kbps).",
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

    status_msg = await message.answer("🔍 Читаю страницу релизов...")
    print("DEBUG_LOG ---> Отправлен статус: Читаю ссылку...")

    temp_dir = tempfile.mkdtemp(prefix="music_bot_")
    try:
        track_title, track_artist = get_universal_metadata(url)
        print(f"DEBUG_LOG ---> Распознано: Артист='{track_artist}' | Трек='{track_title}'")

        if not track_title:
            track_title = url

        search_query = f"{track_artist} - {track_title}" if track_artist else track_title
        
        # Очищаем запрос от лишней разметки
        search_query = search_query.replace(" - - ", " - ").strip()
        if len(search_query) > 80:
            search_query = search_query[:80].strip()

        await status_msg.edit_text(f"🎵 Ищу в каталогах: <b>{search_query}</b>\n⏳ Скачиваем аудио...", parse_mode="HTML")

        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": os.path.join(temp_dir, "audio.%(ext)s"),
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

        # Каскадный поиск ИСКЛЮЧИТЕЛЬНО по музыкальным платформам (без YouTube)
        search_providers = [
            f"scsearch1:{search_query}",       # 1. SoundCloud (основной склад клубной музыки и ремиксов)
            f"bandcampsearch1:{search_query}", # 2. Bandcamp (высокое качество релизов)
            f"vksearch1:{search_query}"        # 3. VK Музыка (резерв)
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
                            potential_file = Path(temp_dir) / "audio.mp3"
                            if potential_file.exists():
                                downloaded_file = potential_file
                                print(f"DEBUG_LOG ---> Успешно найдено и скачано через {search_target}")
                                break
                except Exception as e:
                    print(f"DEBUG_LOG ---> Платформа {search_target} не дала результатов: {e}")
                    continue

        if not downloaded_file or not downloaded_file.exists():
            candidates = list(Path(temp_dir).glob("*.mp3"))
            if not candidates:
                raise RuntimeError("Трек не найден на музыкальных платформах (SoundCloud, Bandcamp, VK).")
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
