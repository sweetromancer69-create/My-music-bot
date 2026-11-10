import os
import re
import json
import time
import uuid
import hmac
import base64
import hashlib
import asyncio
import logging
import tempfile
import subprocess
import urllib.parse
from pathlib import Path
import requests
from bs4 import BeautifulSoup
import yt_dlp
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TBPM, TKEY, TCON, APIC, ID3NoHeaderError
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

AM_CONSUMER_KEY = "audiomack-web"
AM_CONSUMER_SECRET = "bd8a07e9f23fbe9d808646b730f89b8e"


def clean_beatport_title(raw_text: str):
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
        "catalog": "",
        "genre": "",
        "bpm": "",
        "key": "",
        "release_date": "",
        "isrc": "",
        "cover_url": "",
    }
    try:
        resp = requests.get(url, headers=HEADERS, timeout=12)
        if resp.ok:
            soup = BeautifulSoup(resp.text, "html.parser")

            # 1. Точное извлечение из __NEXT_DATA__ (pageProps.track или dehydratedState)
            next_script = soup.find("script", id="__NEXT_DATA__")
            if next_script and next_script.string:
                try:
                    data = json.loads(next_script.string)
                    page_props = data.get("props", {}).get("pageProps", {})

                    # Вариант А: напрямую в pageProps.track (актуальная структура Beatport)
                    t_info = page_props.get("track")
                    if not isinstance(t_info, dict):
                        t_info = None
                        queries = page_props.get("dehydratedState", {}).get("queries", [])
                        for q in queries:
                            sd = q.get("state", {}).get("data", {})
                            if isinstance(sd, dict):
                                cand = sd.get("track") if "track" in sd else sd
                                if isinstance(cand, dict) and (cand.get("name") or cand.get("track_name")):
                                    t_info = cand
                                    break

                    if isinstance(t_info, dict):
                        meta["title"] = t_info.get("track_name") or t_info.get("name") or ""
                        meta["mix"] = t_info.get("mix_name") or ""
                        artists_list = t_info.get("artists", [])
                        if isinstance(artists_list, list):
                            meta["artist"] = ", ".join(
                                [a.get("name", "") for a in artists_list if isinstance(a, dict) and a.get("name")]
                            )
                        if t_info.get("bpm"):
                            meta["bpm"] = str(t_info.get("bpm"))

                        key_val = t_info.get("key")
                        camelot_val = t_info.get("key_camelot")
                        if isinstance(key_val, dict):
                            key_str = key_val.get("name", "")
                        elif isinstance(key_val, str):
                            key_str = key_val
                        else:
                            key_str = ""
                        if key_str and camelot_val:
                            meta["key"] = f"{key_str} ({camelot_val})"
                        else:
                            meta["key"] = key_str or str(camelot_val or "")

                        meta["isrc"] = t_info.get("isrc") or ""

                        genre_obj = t_info.get("genre")
                        if isinstance(genre_obj, dict):
                            meta["genre"] = genre_obj.get("name", "")

                        rel_obj = t_info.get("release") or {}
                        if isinstance(rel_obj, dict):
                            meta["catalog"] = rel_obj.get("catalog_number") or ""
                            r_date = rel_obj.get("release_date") or ""
                            if "T" in r_date:
                                r_date = r_date.split("T")[0]
                            meta["release_date"] = r_date
                            img_u = rel_obj.get("image_url") or ""
                            if not img_u and isinstance(rel_obj.get("image"), dict):
                                img_u = rel_obj["image"].get("uri") or rel_obj["image"].get("dynamic_uri") or ""
                            if img_u:
                                meta["cover_url"] = img_u.replace("{w}", "1400").replace("{h}", "1400")

                        lbl_obj = t_info.get("label") or (rel_obj.get("label") if isinstance(rel_obj, dict) else {})
                        if isinstance(lbl_obj, dict):
                            meta["label"] = lbl_obj.get("name", "")
                except Exception as ex:
                    logger.warning(f"Error parsing Beatport __NEXT_DATA__: {ex}")

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


def audiomack_api_get(path: str, params: dict = None) -> dict:
    params = dict(params or {})
    url = "https://api.audiomack.com/v1" + path
    oauth = {
        "oauth_consumer_key": AM_CONSUMER_KEY,
        "oauth_nonce": uuid.uuid4().hex,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_version": "1.0",
    }
    all_p = {**params, **oauth}
    sorted_p = "&".join(
        f"{urllib.parse.quote(str(k), safe='')}={urllib.parse.quote(str(v), safe='')}"
        for k, v in sorted(all_p.items())
    )
    base_str = f"GET&{urllib.parse.quote(url, safe='')}&{urllib.parse.quote(sorted_p, safe='')}"
    sig = base64.b64encode(
        hmac.new((AM_CONSUMER_SECRET + "&").encode(), base_str.encode(), hashlib.sha1).digest()
    ).decode()
    all_p["oauth_signature"] = sig
    full_url = f"{url}?{urllib.parse.urlencode(all_p)}"
    r = requests.get(full_url, headers=HEADERS, timeout=10)
    r.raise_for_status()
    return r.json()


def normalize_for_match(s: str) -> str:
    s = re.sub(r"\([^\)]*\)|\[[^\]]*\]", "", s.lower())
    s = re.sub(r"[^a-z0-9а-яё\s]", " ", s)
    return " ".join(s.split())


def download_from_audiomack(artist: str, title: str, mix: str, tmp_dir: str) -> str:
    """
    Быстрый поиск и скачивание HQ-потока напрямую с серверов Audiomack (1-2 сек, без блокировок IP).
    """
    base_title = normalize_for_match(title)
    base_artist = normalize_for_match(artist.split(",")[0])

    queries = [f"{artist} {title}".strip()]
    if mix and mix.lower() not in title.lower() and mix.lower() != "original mix":
        queries.insert(0, f"{artist} {title} {mix}".strip())

    for q in queries:
        try:
            search_res = audiomack_api_get("/search", {"q": q, "show": "songs", "limit": 8})
            results = search_res.get("results") or []
            for item in results:
                item_title = normalize_for_match(str(item.get("title") or ""))
                item_artist = normalize_for_match(str(item.get("artist") or item.get("uploader", {}).get("name") or ""))
                duration = int(item.get("duration") or 0)

                # Проверяем совпадение названия трека и длительность (>= 60 сек)
                if base_title and base_title not in item_title and item_title not in base_title:
                    continue
                if duration > 0 and duration < 60:
                    continue

                song_id = item.get("id")
                if not song_id:
                    continue

                play_data = audiomack_api_get(
                    f"/music/play/{song_id}",
                    {"environment": "desktop-web", "hq": "true", "section": "search"},
                )
                stream_url = play_data.get("signedUrl") or play_data.get("url")
                if not stream_url:
                    continue

                logger.info(f"Audiomack matched: {item.get('artist')} - {item.get('title')} ({duration}s)")
                raw_audio_path = os.path.join(tmp_dir, "am_raw.m4a")
                out_mp3_path = os.path.join(tmp_dir, "audio.mp3")

                with requests.get(stream_url, headers=HEADERS, stream=True, timeout=20) as sr:
                    sr.raise_for_status()
                    with open(raw_audio_path, "wb") as f:
                        for chunk in sr.iter_content(chunk_size=65536):
                            if chunk:
                                f.write(chunk)

                # Конвертируем в 320 kbps MP3 через ffmpeg
                cmd = [
                    "ffmpeg", "-y", "-i", raw_audio_path,
                    "-vn", "-ar", "44100", "-ac", "2", "-b:a", "320k",
                    out_mp3_path,
                ]
                subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=True)
                if os.path.exists(out_mp3_path) and os.path.getsize(out_mp3_path) > 100000:
                    return out_mp3_path
        except Exception as e:
            logger.warning(f"Audiomack search/download warning for '{q}': {e}")
            continue

    return ""


def find_bandcamp_track_url(artist: str, title: str) -> str:
    """Ищет прямую ссылку на трек в Bandcamp"""
    try:
        q = urllib.parse.quote(f"{artist} {title}".strip())
        r = requests.get(f"https://bandcamp.com/search?q={q}&item_type=t", headers=HEADERS, timeout=8)
        if r.ok:
            soup = BeautifulSoup(r.text, "html.parser")
            base_title = normalize_for_match(title)
            for res_el in soup.select(".searchresult.track"):
                heading = res_el.select_one(".heading a")
                if heading and heading.get("href"):
                    t_name = normalize_for_match(heading.get_text(strip=True))
                    if not base_title or base_title in t_name or t_name in base_title:
                        return heading["href"].split("?")[0]
    except Exception as e:
        logger.warning(f"Bandcamp search warning: {e}")
    return ""


def embed_tags(file_path: str, meta: dict, cover_bytes: bytes = None):
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
            lbl_text = meta["label"]
            if meta.get("catalog"):
                lbl_text = f"{lbl_text} [{meta['catalog']}]"
            tags["TALB"] = TALB(encoding=3, text=lbl_text)
        if meta.get("bpm"):
            tags["TBPM"] = TBPM(encoding=3, text=str(meta["bpm"]))
        if meta.get("key"):
            tags["TKEY"] = TKEY(encoding=3, text=str(meta["key"]))
        if meta.get("genre"):
            tags["TCON"] = TCON(encoding=3, text=str(meta["genre"]))

        if cover_bytes:
            tags["APIC"] = APIC(
                encoding=3,
                mime="image/jpeg",
                type=3,
                desc="Cover",
                data=cover_bytes,
            )

        tags.save(file_path)
    except Exception as e:
        logger.warning(f"Tagging error: {e}")


def search_and_download(url: str, meta: dict, tmp_dir: str):
    artist = meta.get("artist", "").strip()
    title = meta.get("title", "").strip()
    mix = meta.get("mix", "").strip()

    # Скачиваем обложку заранее (для вшивания в MP3 и превью в Telegram)
    cover_bytes = None
    cover_path = None
    if meta.get("cover_url"):
        try:
            cr = requests.get(meta["cover_url"], headers=HEADERS, timeout=8)
            if cr.ok and len(cr.content) > 1000:
                cover_bytes = cr.content
                cover_path = os.path.join(tmp_dir, "cover.jpg")
                with open(cover_path, "wb") as cf:
                    cf.write(cover_bytes)
        except Exception:
            pass

    # 1. Каскад 1: Audiomack HQ API (мгновенное скачивание за 1.5 сек без блокировок IP)
    if artist or title:
        am_mp3 = download_from_audiomack(artist, title, mix, tmp_dir)
        if am_mp3 and os.path.exists(am_mp3):
            embed_tags(am_mp3, meta, cover_bytes)
            return am_mp3, cover_path

    # 2. Каскад 2: SoundCloud (с проверкой совпадения названия трека) + Bandcamp + YouTube
    out_tmpl = os.path.join(tmp_dir, "audio.%(ext)s")
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": out_tmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 12,
        "retries": 2,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "320",
            }
        ],
    }

    # Если пользователь прислал прямую ссылку на SoundCloud / Bandcamp / Audiomack
    if any(d in url.lower() for d in ("soundcloud.com", "bandcamp.com", "audiomack.com")):
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.extract_info(url, download=True)
        except Exception as e:
            logger.warning(f"Direct download error: {e}")

    candidates = list(Path(tmp_dir).glob("*.mp3"))
    if not candidates:
        base_title = normalize_for_match(title)
        clean_q = f"{artist} {title}".strip()
        mix_q = f"{artist} {title} {mix}".strip() if mix and mix.lower() != "original mix" else clean_q

        # Проверяем топ-5 результатов SoundCloud, чтобы скачать именно этот трек, а не другой трек артиста
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            for sc_q in ([mix_q, clean_q] if mix_q != clean_q else [clean_q]):
                try:
                    sc_info = ydl.extract_info(f"scsearch5:{sc_q}", download=False)
                    entries = sc_info.get("entries") or []
                    for entry in entries:
                        if not entry:
                            continue
                        e_title = normalize_for_match(entry.get("title") or "")
                        e_dur = int(entry.get("duration") or 0)
                        if base_title and base_title in e_title and e_dur >= 60:
                            ep_url = entry.get("webpage_url") or entry.get("url")
                            if ep_url:
                                logger.info(f"SoundCloud verified match: {entry.get('title')} ({e_dur}s)")
                                ydl.extract_info(ep_url, download=True)
                                break
                    if list(Path(tmp_dir).glob("*.mp3")):
                        break
                except Exception as e:
                    logger.warning(f"SoundCloud search warning: {e}")

            # Если не нашли в SoundCloud — пробуем Bandcamp и YouTube
            if not list(Path(tmp_dir).glob("*.mp3")):
                bc_url = find_bandcamp_track_url(artist, title)
                fallback_targets = ([bc_url] if bc_url else []) + [f"ytsearch1:{mix_q}"]
                for target in fallback_targets:
                    try:
                        logger.info(f"Trying fallback target: {target}")
                        ydl.extract_info(target, download=True)
                        if list(Path(tmp_dir).glob("*.mp3")):
                            break
                    except Exception as e:
                        logger.warning(f"Fallback {target} failed: {e}")

    candidates = list(Path(tmp_dir).glob("*.mp3"))
    if candidates:
        final_file = str(candidates[0])
        embed_tags(final_file, meta, cover_bytes)
        return final_file, cover_path

    raise RuntimeError(f"Трек «{artist} – {title}» не найден в открытых каталогах.")


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "🎧 <b>Musicvibez.org Beatport Downloader</b>\n\n"
        "Отправьте ссылку на трек с <b>Beatport</b>, <b>SoundCloud</b>, <b>Bandcamp</b> или <b>Deezer</b>.\n\n"
        "⚡ Бот моментально найдёт полный трек, выкачает его в 320 kbps MP3, пропишет BPM, тональность (Key), лейбл, каталожный номер и HD-обложку!",
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
    status_msg = await message.answer("🔍 Читаю метаданные релиза...")

    try:
        meta = await asyncio.wait_for(asyncio.to_thread(parse_beatport, url), timeout=15.0)

        if not meta.get("title"):
            slug_match = re.search(r"/track/([^/]+)/(\d+)", url)
            if slug_match:
                meta["title"] = slug_match.group(1).replace("-", " ").title()

        clean_artist = meta.get("artist") or "Various Artists"
        clean_title = meta.get("title") or "Track"
        if meta.get("mix") and meta["mix"].lower() not in clean_title.lower() and meta["mix"] != "Original Mix":
            clean_title = f"{clean_title} ({meta['mix']})"

        info_preview = [f"🎵 <b>{clean_artist} – {clean_title}</b>"]
        if meta.get("label"):
            lbl_line = f"{meta['label']} [{meta['catalog']}]" if meta.get("catalog") else meta["label"]
            info_preview.append(f"🏷 Лейбл: <b>{lbl_line}</b>")
        if meta.get("bpm") or meta.get("key"):
            bk = " | ".join(filter(None, [f"{meta['bpm']} BPM" if meta.get("bpm") else "", meta.get("key") or ""]))
            info_preview.append(f"🎛 <b>{bk}</b>")
        info_preview.append("\n⏳ <i>Скачиваю HQ 320 kbps MP3 и прописываю ID3-теги...</i>")

        await status_msg.edit_text("\n".join(info_preview), parse_mode="HTML")

        with tempfile.TemporaryDirectory() as tmp_dir:
            file_path, cover_path = await asyncio.wait_for(
                asyncio.to_thread(search_and_download, url, meta, tmp_dir),
                timeout=50.0,
            )

            caption_parts = [f"🎵 <b>{clean_artist} – {clean_title}</b>"]
            if meta.get("label"):
                lbl_str = f"{meta['label']} [{meta['catalog']}]" if meta.get("catalog") else meta["label"]
                caption_parts.append(f"🏷 <b>Label:</b> {lbl_str}")
            if meta.get("genre"):
                caption_parts.append(f"💿 <b>Genre:</b> {meta['genre']}")
            if meta.get("bpm"):
                caption_parts.append(f"⚡ <b>BPM:</b> {meta['bpm']}")
            if meta.get("key"):
                caption_parts.append(f"🎹 <b>Key:</b> {meta['key']}")
            if meta.get("release_date"):
                caption_parts.append(f"📅 <b>Date:</b> {meta['release_date']}")
            caption_parts.append("\n🎧 <a href='https://musicvibez.org'>MusicVibez.org</a> • <a href='https://specialfordjs.org'>SpecialForDJs.org</a>")

            clean_filename = f"{clean_artist} - {clean_title}.mp3".replace("/", "_").replace("\\", "_").replace(":", "")
            audio_file = FSInputFile(file_path, filename=clean_filename)
            thumb_file = FSInputFile(cover_path) if cover_path and os.path.exists(cover_path) else None

            await status_msg.edit_text("🚀 Отправляю готовый MP3 в чат...")
            await message.answer_audio(
                audio=audio_file,
                title=clean_title,
                performer=clean_artist,
                thumbnail=thumb_file,
                caption="\n".join(caption_parts),
                parse_mode="HTML",
            )
            await status_msg.delete()

    except asyncio.TimeoutError:
        logger.error("Download timeout exceeded")
        await status_msg.edit_text("⏳ Превышено время ожидания ответа от аудио-сервера. Попробуйте еще раз.")
    except Exception as e:
        logger.exception("Error processing link")
        await status_msg.edit_text(f"❌ Ошибка: {str(e)[:250]}")


async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот успешно запущен и слушает сообщения!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
