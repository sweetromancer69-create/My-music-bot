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

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if ADMIN_ID and message.from_user.id != int(ADMIN_ID):
        await message.answer("У вас нет доступа к этому боту.")
        return

    await message.answer(
        "Привет! Отправь мне ссылку на трек с Beatport, "
        "и я попробую скачать его напрямую."
    )

@dp.message(F.text)
async def handle_beatport_link(message: types.Message):
    if ADMIN_ID and message.from_user.id != int(ADMIN_ID):
        return

    url = message.text.strip()

    if not url.startswith(("http://", "https://")):
        await message.answer("Пожалуйста, отправь корректную ссылку.")
        return

    status_msg = await message.answer("🔍 Проверяю ссылку и скачиваю трек через yt-dlp...")

    temp_dir = tempfile.mkdtemp(prefix="beatport_bot_")
    try:
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
            info = ydl.extract_info(url, download=True)
            
            if "entries" in info:
                info = info["entries"][0]

            downloaded_file = Path(ydl.prepare_filename(info)).with_suffix(".mp3")
            track_title = info.get("title", "Beatport Track")
            track_artist = info.get("uploader") or info.get("artist") or "Beatport Release"

        if not downloaded_file.exists():
            candidates = list(Path(temp_dir).glob("*.mp3"))
            if not candidates:
                raise RuntimeError("Не удалось сохранить аудио файл.")
            downloaded_file = candidates[0]

        # Записываем ID3 теги
        try:
            audio = ID3(str(downloaded_file))
        except Exception:
            audio = ID3()
            
        if track_title:
            audio["TIT2"] = TIT2(encoding=3, text=track_title)
        if track_artist:
            audio["TPE1"] = TPE1(encoding=3, text=track_artist)
        audio.save(str(downloaded_file))

        # Отправляем в Telegram
        audio_input = types.FSInputFile(str(downloaded_file))
        await message.answer_audio(
            audio_input,
            title=track_title,
            performer=track_artist
        )
        await status_msg.delete()

    except yt_dlp.utils.DownloadError as e:
        logging.exception("yt-dlp download error")
        await status_msg.edit_text("❌ Beatport не предоставил доступный для этого запроса аудиопоток/файл. Если это купленная загрузка, скачай официальный файл и отправь его боту.")
    except Exception as e:
        logging.exception("Error processing link")
        await status_msg.edit_text(f"Произошла ошибка: {str(e)[:200]}")

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
