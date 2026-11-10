import os
import re
import json
import time
import uuid
import hmac
import base64
import shutil
import hashlib
import asyncio
import logging
import tempfile
import subprocess
import urllib.parse
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
import yt_dlp

from mutagen.id3 import (
    ID3,
    TIT2,
    TPE1,
    TALB,
    TCON,
    TDRC,
    TPUB,
    TBPM,
    TKEY,
    TSRC,
    TXXX,
    COMM,
    APIC,
    ID3NoHeaderError,
)
from mutagen.flac import FLAC, Picture

# ==================== НАСТРОЙКИ И ЛОГИРОВАНИЕ ====================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("musicvibez_bot")

DEFAULT_BOT_TOKEN = "8881412253:AAEUfdKH8VKEWPCXKk4AK8zH_p27Odi3278"
env_token = os.getenv("BOT_TOKEN", "").strip()
if not env_token or env_token.startswith("8881412253:AAELisPK"):
    BOT_TOKEN = DEFAULT_BOT_TOKEN
else:
    BOT_TOKEN = env_token

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Ключи Audiomack OAuth 1.0 (для поиска треков в высоком качестве)
AM_CONSUMER_KEY = "38a7c6407df7918a2ea6f6631ad036bb"
AM_CONSUMER_SECRET = "8f37bc905e94b2a8d5e0f59220970a2f"

# Хранилище задач перед выбором формата (req_id -> dict)
_PENDING_TRACKS = {}


def format_mm_ss(seconds: int) -> str:
    if not seconds or seconds <= 0:
        return "—"
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"


def normalize_for_match(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"[^\w\s]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


# ==================== ПАРСИНГ BEATPORT, DEEZER, QOBUZ ====================
def clean_beatport_title(raw_text: str):
    if not raw_text:
        return None, None
    txt = re.sub(r"\s*\|\s*Beatport.*$", "", raw_text, flags=re.IGNORECASE)
    txt = re.sub(r"\s*-\s*Music\s*&\s*Downloads\s*on\s*Beatport.*$", "", txt, flags=re.IGNORECASE).strip()
    if " - " in txt:
        parts = txt.split(" - ", 1)
        return parts[0].strip(), parts[1].strip()
    return None, txt.strip()


def parse_beatport(url: str) -> dict:
    meta = {
        "platform": "beatport",
        "track_id": None,
        "artist": None,
        "title": None,
        "mix": None,
        "album": None,
        "label": None,
        "catalog": None,
        "genre": None,
        "bpm": None,
        "key": None,
        "isrc": None,
        "release_date": None,
        "cover_url": None,
        "duration_sec": None,
        "is_exclusive": False,
    }
    m_id = re.search(r"/track/[^/]+/(\d+)", url)
    if m_id:
        meta["track_id"] = m_id.group(1)

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        next_script = soup.find("script", id="__NEXT_DATA__")
        if next_script and next_script.string:
            data = json.loads(next_script.string)
            track = data.get("props", {}).get("pageProps", {}).get("track")
            if isinstance(track, dict):
                meta["track_id"] = str(track.get("id") or meta["track_id"] or "")
                meta["title"] = (track.get("name") or "").strip()
                meta["mix"] = (track.get("mix_name") or "").strip()

                artists = [a.get("name", "").strip() for a in track.get("artists", []) if a.get("name")]
                if artists:
                    meta["artist"] = ", ".join(artists)

                release = track.get("release") or {}
                if isinstance(release, dict):
                    meta["album"] = release.get("name")
                    label = release.get("label") or {}
                    if isinstance(label, dict):
                        meta["label"] = label.get("name")
                    img = release.get("image") or {}
                    if isinstance(img, dict):
                        uri = img.get("uri") or img.get("dynamic_uri") or ""
                        if "{w}" in uri:
                            uri = uri.replace("{w}", "1400").replace("{h}", "1400")
                        meta["cover_url"] = uri

                if not meta["label"] and isinstance(track.get("label"), dict):
                    meta["label"] = track["label"].get("name")

                genre = track.get("genre") or {}
                if isinstance(genre, dict):
                    meta["genre"] = genre.get("name")

                if track.get("bpm"):
                    meta["bpm"] = str(track["bpm"])

                key_obj = track.get("key") or {}
                if isinstance(key_obj, dict) and key_obj.get("name"):
                    meta["key"] = key_obj.get("name")

                meta["isrc"] = track.get("isrc")
                meta["catalog"] = track.get("catalog_number")
                meta["release_date"] = track.get("publish_date") or track.get("new_release_date")
                meta["is_exclusive"] = bool(track.get("exclusive"))

                length_ms = track.get("length_ms")
                if length_ms and isinstance(length_ms, (int, float)):
                    meta["duration_sec"] = int(round(length_ms / 1000.0))

        if not meta["title"]:
            og_title = soup.find("meta", property="og:title")
            raw_t = og_title["content"] if og_title and og_title.get("content") else (soup.title.string if soup.title else "")
            art, tit = clean_beatport_title(raw_t)
            if art and not meta["artist"]:
                meta["artist"] = art
            if tit:
                meta["title"] = tit

        if not meta["cover_url"]:
            og_img = soup.find("meta", property="og:image")
            if og_img and og_img.get("content"):
                meta["cover_url"] = og_img["content"]
    except Exception as e:
        logger.warning(f"Ошибка парсинга Beatport: {e}")

    return meta


def parse_deezer(url: str) -> dict:
    real_url = url.strip()
    if "deezer.page.link" in real_url or "link.deezer.com" in real_url:
        try:
            r_redir = requests.get(real_url, headers=HEADERS, allow_redirects=True, timeout=10)
            real_url = r_redir.url
        except Exception:
            pass
    m = re.search(r"/track/(\d+)", real_url)
    if not m:
        raise ValueError("Не удалось извлечь ID трека из ссылки Deezer.")
    tid = m.group(1)
    meta = {
        "platform": "deezer",
        "track_id": tid,
        "artist": "Unknown Artist",
        "title": f"Deezer Track {tid}",
        "mix": None,
        "album": None,
        "label": None,
        "catalog": None,
        "genre": None,
        "bpm": None,
        "key": None,
        "isrc": None,
        "release_date": None,
        "cover_url": None,
        "duration_sec": None,
        "is_exclusive": False,
    }
    try:
        r = requests.get(f"https://api.deezer.com/track/{tid}", headers=HEADERS, timeout=10)
        if r.status_code == 200:
            d = r.json()
            meta["title"] = d.get("title_short") or d.get("title") or meta["title"]
            meta["mix"] = d.get("title_version", "").strip("()") or None
            if isinstance(d.get("artist"), dict):
                meta["artist"] = d["artist"].get("name") or meta["artist"]
            if isinstance(d.get("album"), dict):
                meta["album"] = d["album"].get("title")
                meta["cover_url"] = d["album"].get("cover_xl") or d["album"].get("cover_big")
            meta["duration_sec"] = int(d.get("duration") or 0) or None
            meta["isrc"] = d.get("isrc")
            meta["bpm"] = str(int(d["bpm"])) if d.get("bpm") else None
            meta["release_date"] = d.get("release_date")
    except Exception as e:
        logger.warning(f"Deezer metadata warning: {e}")
    return meta


def parse_qobuz(url: str) -> dict:
    m = re.search(r"qobuz\.com/(?:[a-z]{2}-[a-z]{2}/)?track/(?:[^/?#]+/)?(\d+)", url, re.I)
    if not m:
        m = re.search(r"/track/(\d+)", url, re.I)
    if not m:
        raise ValueError("Не удалось извлечь ID трека из ссылки Qobuz.")
    tid = m.group(1)
    meta = {
        "platform": "qobuz",
        "track_id": tid,
        "artist": "Unknown Artist",
        "title": f"Qobuz Track {tid}",
        "mix": None,
        "album": None,
        "label": None,
        "catalog": None,
        "genre": None,
        "bpm": None,
        "key": None,
        "isrc": None,
        "release_date": None,
        "cover_url": None,
        "duration_sec": None,
        "is_exclusive": False,
    }
    try:
        r = requests.get(
            "https://www.qobuz.com/api.json/0.2/track/get",
            params={"track_id": tid},
            headers={"X-App-Id": "735532640", **HEADERS},
            timeout=12,
        )
        if r.status_code == 200:
            d = r.json()
            meta["title"] = d.get("title") or meta["title"]
            meta["mix"] = d.get("version") or None
            perf = d.get("performer") or {}
            if isinstance(perf, dict) and perf.get("name"):
                meta["artist"] = perf["name"]
            alb = d.get("album") or {}
            if isinstance(alb, dict):
                meta["album"] = alb.get("title")
                img = alb.get("image") or {}
                if isinstance(img, dict):
                    meta["cover_url"] = img.get("large") or img.get("small")
                lbl = alb.get("label") or {}
                if isinstance(lbl, dict):
                    meta["label"] = lbl.get("name")
                gnr = alb.get("genre") or {}
                if isinstance(gnr, dict):
                    meta["genre"] = gnr.get("name")
                meta["release_date"] = alb.get("release_date_original")
            meta["duration_sec"] = int(d.get("duration") or 0) or None
            meta["isrc"] = d.get("isrc")
    except Exception as e:
        logger.warning(f"Qobuz metadata warning: {e}")
    return meta


# ==================== КОНВЕРТАЦИЯ ФОРМАТОВ И ID3 / FLAC ТЕГИ ====================
def convert_audio_to_format(input_path: str, target_format: str, tmp_dir: str) -> str:
    target_format = target_format.lower().strip()
    out_path = os.path.join(tmp_dir, f"converted_{uuid.uuid4().hex[:8]}.{target_format}")
    if input_path.lower().endswith(f".{target_format}"):
        return input_path

    if not shutil.which("ffmpeg"):
        return input_path

    if target_format == "flac":
        cmd = ["ffmpeg", "-y", "-i", input_path, "-vn", "-c:a", "flac", "-compression_level", "8", out_path]
    elif target_format == "wav":
        cmd = ["ffmpeg", "-y", "-i", input_path, "-vn", "-c:a", "pcm_s16le", "-ar", "44100", out_path]
    else:
        cmd = ["ffmpeg", "-y", "-i", input_path, "-vn", "-c:a", "libmp3lame", "-b:a", "320k", out_path]

    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90)
    if os.path.exists(out_path) and os.path.getsize(out_path) > 50_000:
        return out_path
    return input_path


def embed_tags(file_path: str, meta: dict, cover_bytes: bytes = None):
    ext = os.path.splitext(file_path)[1].lower()
    full_title = meta.get("title") or "Unknown Track"
    if meta.get("mix") and meta["mix"].lower() not in full_title.lower():
        full_title = f"{full_title} ({meta['mix']})"
    artist = meta.get("artist") or "Unknown Artist"
    album = meta.get("album") or full_title

    if ext == ".mp3":
        try:
            try:
                tags = ID3(file_path)
            except ID3NoHeaderError:
                tags = ID3()
            tags.add(TIT2(encoding=3, text=full_title))
            tags.add(TPE1(encoding=3, text=artist))
            tags.add(TALB(encoding=3, text=album))
            if meta.get("genre"):
                tags.add(TCON(encoding=3, text=str(meta["genre"])))
            if meta.get("release_date"):
                tags.add(TDRC(encoding=3, text=str(meta["release_date"])[:10]))
            if meta.get("label"):
                tags.add(TPUB(encoding=3, text=str(meta["label"])))
            if meta.get("bpm"):
                tags.add(TBPM(encoding=3, text=str(meta["bpm"])))
            if meta.get("key"):
                tags.add(TKEY(encoding=3, text=str(meta["key"])))
            if meta.get("isrc"):
                tags.add(TSRC(encoding=3, text=str(meta["isrc"])))
            if meta.get("catalog"):
                tags.add(TXXX(encoding=3, desc="CATALOGNUMBER", text=str(meta["catalog"])))
            tags.add(COMM(encoding=3, lang="eng", desc="", text="Downloaded via @sweet_music_dl_bot (MusicVibez.org)"))
            if cover_bytes:
                tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover_bytes))
            tags.save(file_path, v2_version=3)
        except Exception as e:
            logger.warning(f"MP3 tag error: {e}")

    elif ext == ".flac":
        try:
            audio = FLAC(file_path)
            audio["TITLE"] = full_title
            audio["ARTIST"] = artist
            audio["ALBUM"] = album
            if meta.get("genre"):
                audio["GENRE"] = str(meta["genre"])
            if meta.get("release_date"):
                audio["DATE"] = str(meta["release_date"])[:10]
            if meta.get("label"):
                audio["ORGANIZATION"] = str(meta["label"])
                audio["LABEL"] = str(meta["label"])
            if meta.get("bpm"):
                audio["BPM"] = str(meta["bpm"])
            if meta.get("key"):
                audio["INITIALKEY"] = str(meta["key"])
            if meta.get("isrc"):
                audio["ISRC"] = str(meta["isrc"])
            if meta.get("catalog"):
                audio["CATALOGNUMBER"] = str(meta["catalog"])
            audio["COMMENT"] = "Downloaded via @sweet_music_dl_bot (MusicVibez.org)"
            if cover_bytes:
                pic = Picture()
                pic.type = 3
                pic.mime = "image/jpeg"
                pic.desc = "Front Cover"
                pic.data = cover_bytes
                audio.clear_pictures()
                audio.add_picture(pic)
            audio.save()
        except Exception as e:
            logger.warning(f"FLAC tag error: {e}")


# ==================== ПОИСК И СКАЧИВАНИЕ ====================
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
    r = requests.get(full_url, headers=HEADERS, timeout=12)
    r.raise_for_status()
    return r.json()


def download_from_audiomack(meta: dict, target_format: str, tmp_dir: str, allow_radio_fallback: bool = False):
    artist = meta.get("artist", "").strip()
    title = meta.get("title", "").strip()
    mix = meta.get("mix", "").strip()
    expected_dur = int(meta.get("duration_sec") or 0)
    clean_artist = artist.split(",")[0].strip()

    queries = [
        f"{clean_artist} {title} {mix}".strip(),
        f"{clean_artist} {title} Extended",
        f"{clean_artist} {title}",
    ]
    seen_ids = set()
    radio_candidates = []

    for q in queries:
        try:
            data = audiomack_api_get("/search", {"q": q, "show": "music", "limit": 10})
            results = data.get("results") or []
            req_art = normalize_for_match(clean_artist)
            req_tit = normalize_for_match(title)

            for item in results:
                item_id = item.get("id")
                if not item_id or item_id in seen_ids:
                    continue
                seen_ids.add(item_id)

                res_art = normalize_for_match(item.get("artist") or "")
                res_tit = normalize_for_match(item.get("title") or "")
                dur = int(item.get("duration") or 0)

                if req_art not in res_art and res_art not in req_art:
                    continue
                if req_tit not in res_tit:
                    continue

                stream_url = item.get("stream_url") or item.get("download_url")
                if not stream_url:
                    continue

                if expected_dur > 0 and abs(dur - expected_dur) <= 25:
                    raw_path = os.path.join(tmp_dir, f"am_{item_id}.mp3")
                    with requests.get(stream_url, headers=HEADERS, stream=True, timeout=60) as r:
                        r.raise_for_status()
                        with open(raw_path, "wb") as f:
                            for chunk in r.iter_content(chunk_size=131072):
                                if chunk:
                                    f.write(chunk)
                    return convert_audio_to_format(raw_path, target_format, tmp_dir), dur

                if dur > 60:
                    radio_candidates.append((item, dur, stream_url))
        except Exception:
            pass

    if radio_candidates:
        radio_item, r_dur, r_url = radio_candidates[0]
        if allow_radio_fallback or expected_dur == 0:
            raw_path = os.path.join(tmp_dir, f"am_radio_{radio_item.get('id')}.mp3")
            with requests.get(r_url, headers=HEADERS, stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(raw_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=131072):
                        if chunk:
                            f.write(chunk)
            return convert_audio_to_format(raw_path, target_format, tmp_dir), r_dur
        else:
            raise ValueError(f"EXTENDED_ONLY_ON_BEATPORT|{format_mm_ss(expected_dur)}|{format_mm_ss(r_dur)}")

    return None, 0


def download_from_soundcloud_search(meta: dict, target_format: str, tmp_dir: str, allow_radio_fallback: bool = False):
    artist = meta.get("artist", "").strip()
    title = meta.get("title", "").strip()
    expected_dur = int(meta.get("duration_sec") or 0)
    clean_artist = artist.split(",")[0].strip()

    search_query = f"scsearch5:{clean_artist} {title} Extended Mix" if meta.get("platform") == "beatport" else f"scsearch5:{clean_artist} {title}"
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(tmp_dir, "sc_search_%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 20,
    }

    req_tit = normalize_for_match(title)
    radio_candidate = None

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_query, download=False)
            entries = info.get("entries") or []
            for entry in entries:
                if not entry:
                    continue
                ent_title = entry.get("title") or ""
                ent_uploader = entry.get("uploader") or ""
                dur = int(entry.get("duration") or 0)

                norm_text = normalize_for_match(f"{ent_uploader} {ent_title}")
                if req_tit not in norm_text:
                    continue

                if expected_dur > 0 and abs(dur - expected_dur) <= 25:
                    dl_info = ydl.extract_info(entry["webpage_url"], download=True)
                    dl_file = ydl.prepare_filename(dl_info)
                    return convert_audio_to_format(dl_file, target_format, tmp_dir), dur

                if dur > 60 and not radio_candidate:
                    radio_candidate = (entry, dur)

        if radio_candidate:
            cand_entry, r_dur = radio_candidate
            if allow_radio_fallback or expected_dur == 0:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    dl_info = ydl.extract_info(cand_entry["webpage_url"], download=True)
                    dl_file = ydl.prepare_filename(dl_info)
                    return convert_audio_to_format(dl_file, target_format, tmp_dir), r_dur
            else:
                raise ValueError(f"EXTENDED_ONLY_ON_BEATPORT|{format_mm_ss(expected_dur)}|{format_mm_ss(r_dur)}")
    except ValueError:
        raise
    except Exception as e:
        logger.warning(f"SoundCloud search warning: {e}")

    return None, 0


def process_download_request(url: str, meta: dict, target_format: str, tmp_dir: str, allow_radio_fallback: bool = False):
    actual_dur = meta.get("duration_sec") or 0
    audio_path = None

    # Прямая ссылка на SoundCloud или Bandcamp
    if any(d in url.lower() for d in ("soundcloud.com", "bandcamp.com")):
        out_tmpl = os.path.join(tmp_dir, "direct_%(id)s.%(ext)s")
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": out_tmpl,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 20,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if "entries" in info:
                info = info["entries"][0]
            dl_file = ydl.prepare_filename(info)
            if not meta.get("title"):
                meta["title"] = info.get("track") or info.get("title") or "Track"
            if not meta.get("artist"):
                meta["artist"] = info.get("artist") or info.get("uploader") or "Artist"
            if not meta.get("cover_url"):
                meta["cover_url"] = info.get("thumbnail")
            actual_dur = int(info.get("duration") or 0)
            audio_path = convert_audio_to_format(dl_file, target_format, tmp_dir)
    else:
        # Поиск трека (Beatport Extended / Deezer / Qobuz)
        audio_path, actual_dur = download_from_audiomack(meta, target_format, tmp_dir, allow_radio_fallback)
        if not audio_path:
            audio_path, actual_dur = download_from_soundcloud_search(meta, target_format, tmp_dir, allow_radio_fallback)

        if not audio_path:
            clean_art = meta.get("artist") or "Artist"
            clean_tit = meta.get("title") or "Track"
            raise ValueError(f"Трек <b>{clean_art} – {clean_tit}</b> не найден в открытых музыкальных базах.")

    # Скачиваем оригинальную обложку релиза и вшиваем все метаданные
    cover_bytes = None
    cover_path = None
    if meta.get("cover_url"):
        try:
            cr = requests.get(meta["cover_url"], headers=HEADERS, timeout=12)
            if cr.status_code == 200 and len(cr.content) > 1000:
                cover_bytes = cr.content
                cover_path = os.path.join(tmp_dir, "cover.jpg")
                with open(cover_path, "wb") as f:
                    f.write(cover_bytes)
        except Exception:
            pass

    embed_tags(audio_path, meta, cover_bytes)
    return audio_path, cover_path, actual_dur


# ==================== TELEGRAM HANDLERS ====================
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 <b>Привет! Я бот для скачивания треков для DJ:</b>\n\n"
        "• <b>Beatport</b> (парсинг BPM, тональности Key, лейбла и поиск полных <b>Extended Mix</b>)\n"
        "• <b>Deezer</b> и <b>Qobuz</b> (поиск треков и скачивание в выбранном формате)\n"
        "• <b>SoundCloud / Bandcamp</b> (по прямой ссылке)\n\n"
        "💎 <b>Никаких капч, привязок карт и подтверждений!</b>\n"
        "Отправьте ссылку на трек, выберите формат (<b>MP3 320 / FLAC / WAV</b>) и бот пришлёт файл.",
        parse_mode="HTML",
    )


@dp.message(F.text)
async def handle_link(message: types.Message):
    text = message.text.strip()
    url_match = re.search(r"(https?://\S+)", text)
    if not url_match:
        await message.answer(
            "Отправьте ссылку на трек с <b>Beatport</b>, <b>Deezer</b>, <b>Qobuz</b> или <b>SoundCloud</b>.",
            parse_mode="HTML",
        )
        return

    url = url_match.group(1).strip()
    low_url = url.lower()

    # Прямая ссылка на SoundCloud / Bandcamp
    if any(d in low_url for d in ("soundcloud.com", "bandcamp.com")):
        req_id = uuid.uuid4().hex[:10]
        _PENDING_TRACKS[req_id] = {"url": url, "meta": {"platform": "soundcloud", "title": None, "artist": None}}
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🎧 MP3 (320 kbps)", callback_data=f"dl|{req_id}|mp3|0"),
                    InlineKeyboardButton(text="💎 FLAC (Lossless)", callback_data=f"dl|{req_id}|flac|0"),
                    InlineKeyboardButton(text="💿 WAV", callback_data=f"dl|{req_id}|wav|0"),
                ]
            ]
        )
        await message.answer(
            "☁️ <b>Прямая ссылка SoundCloud / Bandcamp</b>\nВыберите формат для скачивания:",
            reply_markup=kb,
            parse_mode="HTML",
        )
        return

    status_msg = await message.answer("🔍 Читаю данные трека...")
    try:
        if "beatport.com" in low_url:
            meta = await asyncio.wait_for(asyncio.to_thread(parse_beatport, url), timeout=15.0)
            plat_badge = "🎧 <b>Beatport Track</b>"
        elif "deezer." in low_url or "deezer.page.link" in low_url:
            meta = await asyncio.wait_for(asyncio.to_thread(parse_deezer, url), timeout=15.0)
            plat_badge = "💜 <b>Deezer Track</b>"
        elif "qobuz.com" in low_url:
            meta = await asyncio.wait_for(asyncio.to_thread(parse_qobuz, url), timeout=15.0)
            plat_badge = "💿 <b>Qobuz Track</b>"
        else:
            await status_msg.edit_text(
                "⚠️ Поддерживаются ссылки на <b>Beatport</b>, <b>Deezer</b>, <b>Qobuz</b> и <b>SoundCloud</b>.",
                parse_mode="HTML",
            )
            return
    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка разбора ссылки: {e}")
        return

    clean_artist = meta.get("artist") or "Various Artists"
    clean_title = meta.get("title") or "Track"
    if meta.get("mix") and meta["mix"].lower() not in clean_title.lower():
        clean_title = f"{clean_title} ({meta['mix']})"

    req_id = uuid.uuid4().hex[:10]
    _PENDING_TRACKS[req_id] = {"url": url, "meta": meta}

    dur_str = format_mm_ss(meta.get("duration_sec") or 0)
    excl_badge = " 🔒 <i>(Beatport Exclusive)</i>" if meta.get("is_exclusive") else ""

    lines = [
        plat_badge,
        f"🎵 <b>{clean_artist} – {clean_title}</b>{excl_badge}",
    ]
    if meta.get("label"):
        lbl_str = f"{meta['label']} [{meta['catalog']}]" if meta.get("catalog") else meta["label"]
        lines.append(f"🏷 <b>Label:</b> {lbl_str}")
    if meta.get("genre"):
        lines.append(f"💿 <b>Genre:</b> {meta['genre']}")
    if meta.get("bpm") or meta.get("key"):
        lines.append(f"⚡ <b>BPM:</b> {meta.get('bpm') or '—'} | 🎹 <b>Key:</b> {meta.get('key') or '—'}")
    if meta.get("duration_sec"):
        lines.append(f"⏱ <b>Длительность:</b> {dur_str}")
    lines.append("\n👇 <b>Выберите формат:</b>")

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🎧 MP3 (320 kbps)", callback_data=f"dl|{req_id}|mp3|0"),
                InlineKeyboardButton(text="💎 FLAC (Lossless)", callback_data=f"dl|{req_id}|flac|0"),
                InlineKeyboardButton(text="💿 WAV", callback_data=f"dl|{req_id}|wav|0"),
            ]
        ]
    )
    await status_msg.edit_text("\n".join(lines), reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data.startswith("dl|"))
async def handle_format_callback(call: CallbackQuery):
    parts = call.data.split("|")
    if len(parts) < 4:
        await call.answer("Устаревшая кнопка")
        return
    _, req_id, fmt, allow_radio_flag = parts
    allow_radio = allow_radio_flag == "1"

    task_data = _PENDING_TRACKS.get(req_id)
    if not task_data:
        await call.answer("Сессия трека устарела, отправьте ссылку заново.", show_alert=True)
        return

    url = task_data["url"]
    meta = task_data["meta"]
    clean_artist = meta.get("artist") or "Artist"
    clean_title = meta.get("title") or "Track"
    if meta.get("mix") and meta["mix"].lower() not in clean_title.lower():
        clean_title = f"{clean_title} ({meta['mix']})"

    await call.answer(f"Готовлю {fmt.upper()}...")
    await call.message.edit_text(
        f"⏳ <b>{clean_artist} – {clean_title}</b>\n"
        f"Формат: <b>{fmt.upper()}</b> | Поиск оригинальной версии...",
        parse_mode="HTML",
    )

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            file_path, cover_path, actual_dur = await asyncio.wait_for(
                asyncio.to_thread(process_download_request, url, meta, fmt, tmp_dir, allow_radio),
                timeout=90.0,
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
            if actual_dur:
                caption_parts.append(f"⏱ <b>Length:</b> {format_mm_ss(actual_dur)} ({fmt.upper()})")
            caption_parts.append(
                "\n🎧 <a href='https://musicvibez.org'>MusicVibez.org</a> • <a href='https://specialfordjs.org'>SpecialForDJs.org</a>"
            )

            ext = fmt.lower()
            clean_filename = f"{clean_artist} - {clean_title}.{ext}".replace("/", "_").replace("\\", "_").replace(":", "")
            audio_file = FSInputFile(file_path, filename=clean_filename)
            thumb_file = FSInputFile(cover_path) if cover_path and os.path.exists(cover_path) else None

            await call.message.edit_text(f"🚀 Выгружаю <b>{fmt.upper()}</b> в Telegram...", parse_mode="HTML")
            await call.message.answer_audio(
                audio=audio_file,
                title=clean_title,
                performer=clean_artist,
                duration=actual_dur if actual_dur and actual_dur > 0 else None,
                thumbnail=thumb_file,
                caption="\n".join(caption_parts),
                parse_mode="HTML",
            )
            await call.message.delete()
    except ValueError as ve:
        msg_str = str(ve)
        if msg_str.startswith("EXTENDED_ONLY_ON_BEATPORT|"):
            _, bp_dur, radio_dur = msg_str.split("|")
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=f"⬇️ Всё равно скачать Radio Edit ({radio_dur}) в {fmt.upper()}",
                            callback_data=f"dl|{req_id}|{fmt}|1",
                        )
                    ]
                ]
            )
            await call.message.edit_text(
                f"⚠️ <b>Внимание: защита от урезанных Radio Edit!</b>\n\n"
                f"Трек <b>{clean_artist} – {clean_title}</b> на Beatport имеет длительность <b>{bp_dur} (Extended Mix)</b>, "
                f"а в открытых базах лейбл выпустил только урезанную <b>Radio Edit ({radio_dur})</b>.\n\n"
                f"Бот остановил скачивание, чтобы не портить сет урезанным треком.\n"
                f"Если вам подходит версия {radio_dur}, нажмите кнопку ниже:",
                reply_markup=kb,
                parse_mode="HTML",
            )
        else:
            await call.message.edit_text(f"❌ {msg_str}", parse_mode="HTML")
    except Exception as e:
        logger.exception("Download callback error")
        await call.message.edit_text(f"❌ Ошибка скачивания: {str(e)[:350]}", parse_mode="HTML")


# ==================== HTTP HEALTHCHECK ДЛЯ BLITZ CLOUD ====================
async def start_healthcheck_server():
    app = web.Application()
    app.router.add_get("/", lambda req: web.json_response({"ok": True, "bot": "sweet_music_dl_bot"}))
    app.router.add_get("/health", lambda req: web.json_response({"ok": True}))
    runner = web.AppRunner(app)
    await runner.setup()
    ports = {int(os.environ.get("PORT", "8080")), 8080, 8000, 3000}
    for p in ports:
        try:
            site = web.TCPSite(runner, "0.0.0.0", p)
            await site.start()
            logger.info(f"Healthcheck HTTP server listening on 0.0.0.0:{p}")
        except Exception:
            pass


async def main():
    await start_healthcheck_server()
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот успешно запущен и слушает сообщения!")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())
