import os
import asyncio
import logging
import tempfile
from pathlib import Path

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


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "Привет! Отправь ссылку на аудио.\n\n"
        "Поддерживаемые yt-dlp источники обрабатываются напрямую. "
        "Для Beatport/Qobuz нужен доступ к разрешённому скачиванию."
    )


@dp.message(F.text)
async def handle_url(message: types.Message):
    url = message.text.strip()

    if not url.startswith(("http://", "https://")):
        await message.answer("Отправь корректную ссылку.")
        return

    await message.answer("Проверяю ссылку и подготавливаю аудио...")

    temp_dir = tempfile.mkdtemp(prefix="musicbot_")
    mp3_file = None

    try:
        # Не превращаем Beatport/Qobuz в поиск YouTube.
        with yt_dlp.YoutubeDL(get_ydl_options(temp_dir)) as ydl:
            info = ydl.extract_info(url, download=True)

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
            # Иногда имя после postprocessor отличается.
            candidates = list(Path(temp_dir).glob("*.mp3"))

            if not candidates:
                raise RuntimeError(
                    "Источник не предоставил доступный аудиофайл."
                )

            mp3_file = candidates[0]

        audio_file = types.FSInputFile(str(mp3_file))

        await message.answer_audio(
            audio_file,
            title=info.get("title"),
            performer=info.get("artist") or info.get("uploader"),
        )

    except Exception as e:
        logging.exception("Download error")

        error_text = str(e)

        if "Beatport" in error_text:
            await message.answer(
                "Beatport не предоставил доступный для этого запроса "
                "аудиопоток/файл. Если это купленная загрузка, "
                "скачай официальный файл и отправь его боту."
            )
        elif "Qobuz" in error_text:
            await message.answer(
                "Qobuz не предоставил доступный для этого запроса "
                "файл. Для купленных загрузок используй официальный "
                "Download из My Purchases."
            )
        else:
            await message.answer(
                "Не удалось скачать аудио.\n\n"
                f"{error_text[:700]}"
            )

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
