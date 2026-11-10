import os
import re
import json
import asyncio
import logging
import tempfile
import urllib.parse
from pathlib import Path
import requests
from bs4 import BeautifulSoup
import yt_dlp
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TBPM, TKEY, APIC, ID3NoHeaderError
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import FSInputFile

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ==================== НАСТРОЙКИ ИЗ ОКРУЖЕНИЯ ====================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.environ.get("ADMIN_ID", "963491961"))
# ===============================================================

if not BOT_TOKEN:
    raise RuntimeError("ОШИБКА: Переменная BOT_TOKEN не задана в окружении!")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}


def clean_beatport_title(raw_text: str):
    """
    Разбивает заголовок Beatport:
    'John Monkman - Deform (Extended Mix) [Beesemyer Music] | Music & Downloads on Beatport'
    на (artist, title, label)
    """
    cleaned = raw_text
    cleaned = re.sub(r'\|\s*Music\s*&?\s*Downloads\s*on\s*Beatport.*$', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\|\s*Beatport.*$', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s+on\s+Beatport.*$', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s+\|\s+Qobuz.*$', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s+\|\s+Deezer.*$', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s+\|\s+SoundCloud.*$', '', cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip()

    label = ""
    label_match = re.search(r'\[([^\]]+)\]\s*$', cleaned)
    if label_match:
        label = label_match.group(1).strip()
        cleaned = re.sub(r'\s*\[[^\]]+\]\s*$', '', cleaned).strip()

    artist = ""
    title = cleaned
    if " - " in cleaned:
        parts = cleaned.split(" - ", 1)
        artist = parts[0].strip()
        title = parts[1].strip()

    return artist, title, label


def parse_beatport(url: str) -> dict:
    meta = {
        "title": "",
        "artist": "",
        "mix": "",
        "label": "",
        "bpm": "",
        "key": "",
        "cover_url": "",
    }
    try:
        resp = requests.get(url, headers=HEADERS, timeout=12)
        if resp.ok:
            soup = BeautifulSoup(resp.text, "html.parser")

            # 1. Попытка извлечь JSON через __NEXT_DATA__
            next_script = soup.find("script", id="__NEXT_DATA__")
            if next_script and next_script.string:
                try:
                    data = json.loads(next_script.string)
                    queries = (
                        data.get("props", {})
                        .get("pageProps", {})
                        .get("dehydratedState", {})
                        .get("queries", [])
                    )
                    for q in queries:
                        state_data = q.get("state", {}).get("data", {})
                        if isinstance(state_data, dict):
                            t_info = state_data.get("track") if "track" in state_data else state_data
                            if t_info and (t_info.get("name") or t_info.get("title")):
                                meta["title"] = t_info.get("name") or t_info.get("title") or ""
                                meta["mix"] = t_info.get("mix_name") or ""
                                artists_list = t_info.get("artists", [])
                                if isinstance(artists_list, list):
                                    meta["artist"] = ", ".join([a.get("name", "") for a in artists_list if isinstance(a, dict) and a.get("name")])
                                meta["bpm"] = str(t_info.get("bpm") or "")
                                key_data = t_info.get("key")
                                if isinstance(key_data, dict):
                                    meta["key"] = key_data.get("name", "")
                                elif isinstance(key_data, str):
                                    meta["key"] = key_data

                                rel = t_info.get("release") or {}
                                lbl = rel.get("label") or t_info.get("label") or {}
                                if isinstance(lbl, dict):
                                    meta["label"] = lbl.get("name", "")

                                img = rel.get("image") or t_info.get("image") or {}
                                if isinstance(img, dict):
                                    uri = img.get("uri") or img.get("dynamic_uri", "")
                                    if uri:
                                        meta["cover_url"] = uri.replace("{w}", "1400").replace("{h}", "1400")
                                break
                except Exception as ex:
                    logger.warning(f"Error parsing __NEXT_DATA__: {ex}")

            # 2. Резервный парсинг через og:title и og:image
            og_title = soup.find("meta", property="og:title") or soup.find("meta", attrs={"name": "og:title"})
            if og_title and og_title.get("content"):
                parsed_artist, parsed_title, parsed_label = clean_beatport_title(og_title["content"])
                if not meta["title"]:
                    meta["title"] = parsed_title
                if not meta["artist"]:
                    meta["artist"] = parsed_artist
                if not meta["label"]:
                    meta["label"] = parsed_label

            og_img = soup.find("meta", property="og:image") or soup.find("meta", attrs={"name": "og:image"})
            if og_img and og_img.get("content") and not meta["cover_url"]:
                meta["cover_url"] = og_img["content"]

    except Exception as e:
        logger.warning(f"Beatport parse error: {e}")

    return meta


def find_direct_youtube_url(artist: str, title: str, mix: str) -> str:
    """
    Быстрый поиск ID видео через прямой запрос к YouTube Search (менее 1 сек).
    Позволяет yt-dlp скачивать напрямую без зависания на внутреннем поиске.
    """
    search_terms = []
    clean_mix = mix.strip() if mix and mix.lower() != "original mix" else ""
    if clean_mix and clean_mix.lower() not in title.lower():
        search_terms.append(f"{artist} - {title} {clean_mix}")
    search_terms.append(f"{artist} - {title}")
    search_terms.append(f"{artist} {title}")

    for term in search_terms:
        encoded = urllib.parse.quote(term)
        search_url = f"https://www.youtube.com/results?search_query={encoded}"
        try:
            r = requests.get(search_url, headers=HEADERS, timeout=6)
            if r.ok:
                vids = re.findall(r'"videoId":"([a-zA-Z0-9_-]{11})"', r.text)
                if vids:
                    # Исключаем дубли
                    seen = set()
                    unique_vids = [x for x in vids if not (x in seen or seen.add(x))]
                    if unique_vids:
                        logger.info(f"Fast YouTube search found video: {unique_vids[0]} for query: {term}")
                        return f"https://www.youtube.com/watch?v={unique_vids[0]}"
        except Exception as e:
            logger.warning(f"Fast YouTube search error on '{term}': {e}")
            continue

    return ""


def embed_tags(file_path: str, meta: dict):
    try:
        try:
            tags = ID3(file_path)
        except ID3NoHeaderError:
            tags = ID3()

        full_title = meta.get("title", "Track")
        if meta.get("mix") and meta["mix"].lower() not in full_title.lower() and meta["mix"] != "Original Mix":
            full_title = f"{full_title} ({meta['mix']})"

        tags["TIT2"] = TIT2(encoding=3, text=full_title)
        if meta.get("artist"):
            tags["TPE1"] = TPE1(encoding=3, text=meta["artist"])
        if meta.get("label"):
            tags["TALB"] = TALB(encoding=3, text=meta["label"])
        if meta.get("bpm"):
            tags["TBPM"] = TBPM(encoding=3, text=str(meta["bpm"]))
        if meta.get("key"):
            tags["TKEY"] = TKEY(encoding=3, text=str(meta["key"]))

        if meta.get("cover_url"):
            try:
                img_r = requests.get(meta["cover_url"], headers=HEADERS, timeout=8)
                if img_r.ok:
                    tags["APIC"] = APIC(
                        encoding=3,
                        mime="image/jpeg",
                        type=3,
                        desc="Cover",
                        data=img_r.content,
                    )
            except Exception:
                pass

        tags.save(file_path)
    except Exception as e:
        logger.warning(f"Tagging error: {e}")


def search_and_download(meta: dict, tmp_dir: str) -> str:
    artist = meta.get("artist", "").strip()
    title = meta.get("title", "").strip()
    mix = meta.get("mix", "").strip()

    out_tmpl = os.path.join(tmp_dir, "audio.%(ext)s")

    # yt-dlp опции со строгим таймаутом, чтобы процесс никогда не зависал
    ydl_opts = {
        "format": "bestaudio[ext=mp3]/bestaudio/best",
        "outtmpl": out_tmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 12,
        "retries": 2,
        "fragment_retries": 2,
        "http_headers": HEADERS,
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "ios", "web"]
            }
        },
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "320",
            }
        ],
    }

    # 1. Сначала пробуем прямой YouTube URL, найденный за 1 секунду
    direct_yt = find_direct_youtube_url(artist, title, mix)
    downloaded = False

    targets = []
    if direct_yt:
        targets.append(direct_yt)

    # 2. Резервные запросы (SoundCloud и прямой scsearch)
    clean_q = f"{artist} {title}".strip()
    targets.append(f"scsearch1:{clean_q}")
    targets.append(f"scsearch1:{artist} {title} {mix}".strip())
    targets.append(f"vksearch1:{clean_q}")

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        for target in targets:
            try:
                logger.info(f"Downloading from target: {target}")
                info = ydl.extract_info(target, download=True)
                if info:
                    downloaded = True
                    break
            except Exception as err:
                logger.warning(f"Target {target} failed: {err}")
                continue

    # Проверяем скачанный файл
    candidates = list(Path(tmp_dir).glob("*.mp3"))
    if not candidates:
        candidates = list(Path(tmp_dir).glob("audio.*"))

    if candidates:
        final_file = str(candidates[0])
        embed_tags(final_file, meta)
        return final_file

    raise RuntimeError(f"Не удалось выкачать аудио для {artist} – {title}")


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "🎧 <b>Musicvibez.org Beatport Downloader</b>\n\n"
        "Отправьте мне ссылку на трек с <b>Beatport</b>, <b>SoundCloud</b> или <b>Deezer</b>.\n\n"
        "⚡ Бот моментально найдёт полный трек, выкачает его в 320 kbps MP3, пропишет BPM, тональность, лейбл и обложку!",
        parse_mode="HTML",
    )


@dp.message(F.text)
async def handle_link(message: types.Message):
    text = (message.text or "").strip()
    url_match = re.search(r"(https?://\S+)", text)
    if not url_match:
        await message.answer("Пожалуйста, отправьте ссылку на трек с Beatport, SoundCloud или Deezer.")
        return

    url = url_match.group(1)
    status_msg = await message.answer("🔍 Читаю страницу Beatport...")

    try:
        # Парсим метаданные (не более 12 сек)
        meta = await asyncio.wait_for(asyncio.to_thread(parse_beatport, url), timeout=15.0)

        if not meta.get("title"):
            slug_match = re.search(r"/track/([^/]+)/(\d+)", url)
            if slug_match:
                meta["title"] = slug_match.group(1).replace("-", " ").title()

        clean_artist = meta.get("artist") or "Various Artists"
        clean_title = meta.get("title") or "Track"
        if meta.get("mix") and meta["mix"].lower() not in clean_title.lower() and meta["mix"] != "Original Mix":
            clean_title = f"{clean_title} ({meta['mix']})"

        await status_msg.edit_text(
            f"🎵 Найдено: <b>{clean_artist} – {clean_title}</b>\n"
            f"⚡ Ищу аудиопоток и скачиваю трек...",
            parse_mode="HTML",
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            # Ограничиваем скачивание таймаутом 45 сек, чтобы бот никогда не висел!
            file_path = await asyncio.wait_for(
                asyncio.to_thread(search_and_download, meta, tmp_dir),
                timeout=45.0,
            )

            caption_parts = [f"🎵 <b>{clean_artist} – {clean_title}</b>"]
            if meta.get("label"):
                caption_parts.append(f"🏷 <b>Label:</b> {meta['label']}")
            if meta.get("bpm"):
                caption_parts.append(f"⚡ <b>BPM:</b> {meta['bpm']}")
            if meta.get("key"):
                caption_parts.append(f"🎹 <b>Key:</b> {meta['key']}")
            caption_parts.append("\n🎧 <i>Downloaded via @sweet_music_dl_bot</i>")

            clean_filename = f"{clean_artist} - {clean_title}.mp3".replace("/", "_").replace("\\", "_").replace(":", "")
            audio_file = FSInputFile(file_path, filename=clean_filename)

            await status_msg.edit_text("🚀 Отправляю аудиофайл в Telegram...")
            await message.answer_audio(
                audio=audio_file,
                title=clean_title,
                performer=clean_artist,
                caption="\n".join(caption_parts),
                parse_mode="HTML",
            )
            await status_msg.delete()

    except asyncio.TimeoutError:
        logger.error("Download timeout exceeded")
        await status_msg.edit_text("⏳ Время ожидания ответа аудио-серверов истекло. Попробуйте еще раз.")
    except Exception as e:
        logger.exception("Error processing link")
        await status_msg.edit_text(f"❌ Ошибка: {str(e)[:250]}")


async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот успешно запущен и слушает сообщения!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
