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

def get_beatport_metadata(url: str):
    """Достаем название трека и артиста исключительно через парсинг страницы (без yt-dlp)"""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            
            og_title = soup.find("meta", property="og:title")
            if og_title and og_title.get("content"):
                full_text = og_title["content"]
                if " on Beatport" in full_text:
                    full_text = full_text.split(" on Beatport")[0]
                return full_text, ""
    except Exception as e:
        logging.error(f"Ошибка парсинга страницы: {e}")

    return None, None

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if ADMIN_ID and message.from_user.id != int(ADMIN_ID):
        await message.answer("У вас нет доступа к этому боту.")
        return

    await message.answer(
        "Привет! Отправь мне ссылку на трек с Beatport, "
        "и я найду его по названию, оформлю теги и пришлю MP3."
    )

@dp.message(F.text)
async def handle_beatport_link(message: types.Message):
    if ADMIN_ID and message.from_user.id != int(ADMIN_ID):
        return

    url = message.text.strip()

    if not url.startswith(("http://", "https://")):
        await message.answer("Пожалуйста, отправь корректную ссылку.")
        return

    status_msg = await message.answer("🔍 Читаю страницу Beatport...")

    temp_dir = tempfile.mkdtemp(prefix="beatport_bot_")
    try:
        track_title, track_artist = get_beatport_metadata(url)

        if not track_title:
            await status_msg.edit_text("Не удалось распознать трек по ссылке.")
            return

        search_query = f"{track_artist} - {track_title}" if track_artist else track_title
        await status_msg.edit_text(f"🎵 Найдено: <b>{search_query}</b>\n⏳ Ищу и скачиваю аудио...", parse_mode="HTML")

        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": os.path.join(temp_dir, "%(id)s.%(ext)s"),
            "noplaylist": True,
            "quiet": True,
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

        try:
            audio = ID3(str(downloaded_file))
        except Exception:
            audio = ID3()
            
        if track_title:
            audio["TIT2"] = TIT2(encoding=3, text=track_title)
        if track_artist:
            audio["TPE1"] = TPE1(encoding=3, text=track_artist)
        audio.save(str(downloaded_file))

        audio_input = types.FSInputFile(str(downloaded_file))
        await message.answer_audio(
            audio_input,
            title=track_title,
            performer=track_artist or "Beatport Release"
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
