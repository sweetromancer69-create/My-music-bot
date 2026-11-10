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
env_token = (os.getenv("BOT_TOKEN") or "").strip()
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

# Ключи Audiomack OAuth 1.0 (для поиска треков)
AM_CONSUMER_KEY = "38a7c6407df7918a2ea6f6631ad036bb"
AM_CONSUMER_SECRET = "8f37bc905e94b2a8d5e0f59220970a2f"

# Хранилище задач перед выбором формата и треков (id -> dict)
_PENDING_TASKS = {}


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


def clean_beatport_title(raw_text: str):
    if not raw_text:
        return None, None
    txt = re.sub(r"\s*\|\s*Beatport.*$", "", raw_text, flags=re.IGNORECASE)
    txt = re.sub(r"\s*-\s*Music\s*&\s*Downloads\s*on\s*Beatport.*$", "", txt, flags=re.IGNORECASE).strip()
    txt = re.sub(r"\s*\|\s*Music\s*&\s*Downloads\s*on\s*Beatport.*$", "", txt, flags=re.IGNORECASE).strip()
    if " - " in txt:
        parts = txt.split(" - ", 1)
        p0 = (parts[0] or "").strip()
        p1 = (parts[1] or "").strip()
        return p0 or None, p1 or None
    return None, txt.strip() or None


# ==================== ПАРСИНГ BEATPORT, DEEZER, QOBUZ ====================
def parse_beatport_track_data(track: dict, release: dict = None, default_cover: str = None) -> dict:
    t_name = (track.get("track_name") or track.get("name") or track.get("title") or "").strip()
    t_mix = (track.get("mix_name") or "").strip()
    t_id = str(track.get("track_id") or track.get("id") or "")

    artists = [
        (a.get("name") or "").strip()
        for a in track.get("artists", [])
        if isinstance(a, dict) and a.get("name")
    ]
    artist = ", ".join(artists) if artists else "Unknown Artist"

    rel = release or track.get("release") or {}
    album = (rel.get("name") or rel.get("title") or "").strip() or None

    label_obj = track.get("label") or rel.get("label") or {}
    label = label_obj.get("name") if isinstance(label_obj, dict) else (str(label_obj) if label_obj else None)

    catalog = track.get("catalog_number") or rel.get("catalog_number")
    pub_date = track.get("publish_date") or track.get("new_release_date") or rel.get("publish_date") or rel.get("new_release_date")

    img_obj = rel.get("image") or track.get("image") or {}
    cover_url = default_cover or ""
    if isinstance(img_obj, dict):
        uri = img_obj.get("uri") or img_obj.get("dynamic_uri") or rel.get("image_url") or ""
        if "{w}" in uri:
            uri = uri.replace("{w}", "1400").replace("{h}", "1400")
        if uri:
            cover_url = uri

    genre_obj = track.get("genre") or {}
    genre = genre_obj.get("name") if isinstance(genre_obj, dict) else (str(genre_obj) if genre_obj else None)

    bpm = str(track.get("bpm")) if track.get("bpm") else None
    key_val = track.get("key")
    if isinstance(key_val, dict):
        key = key_val.get("name")
    else:
        key = str(key_val) if key_val else None

    camelot = track.get("key_camelot")
    if camelot and key:
        key_display = f"{key} ({camelot})"
    else:
        key_display = key or camelot

    len_ms = track.get("track_length_ms") or track.get("length_ms") or 0
    dur_sec = int(round(len_ms / 1000.0)) if len_ms else None

    return {
        "is_release": False,
        "platform": "beatport",
        "track_id": t_id,
        "artist": artist,
        "title": t_name or "Track",
        "mix": t_mix or None,
        "album": album,
        "label": label,
        "catalog": catalog,
        "genre": genre,
        "bpm": bpm,
        "key": key_display,
        "isrc": track.get("isrc"),
        "release_date": pub_date,
        "cover_url": cover_url or None,
        "duration_sec": dur_sec,
        "is_exclusive": bool(track.get("exclusive")),
    }


def parse_beatport_release(url: str, html: str, data: dict, soup: BeautifulSoup) -> dict:
    props = data.get("props", {}).get("pageProps", {})
    rel_raw = props.get("release") or {}

    rel_name = (rel_raw.get("name") or "Release").strip()
    artists_list = [
        (a.get("name") or "").strip()
        for a in rel_raw.get("artists", [])
        if isinstance(a, dict) and a.get("name")
    ]
    rel_artist = ", ".join(artists_list) if artists_list else "Various Artists"

    label_obj = rel_raw.get("label") or {}
    label_name = label_obj.get("name") if isinstance(label_obj, dict) else None
    catalog = rel_raw.get("catalog_number")
    pub_date = rel_raw.get("publish_date") or rel_raw.get("new_release_date")

    img_obj = rel_raw.get("image") or {}
    cover_uri = img_obj.get("uri") or img_obj.get("dynamic_uri") or rel_raw.get("image_url") or ""
    if "{w}" in cover_uri:
        cover_uri = cover_uri.replace("{w}", "1400").replace("{h}", "1400")

    if not cover_uri:
        og_img = soup.find("meta", property="og:image")
        if og_img and og_img.get("content"):
            cover_uri = og_img["content"].replace("{w}", "1400").replace("{h}", "1400")

    release_meta = {
        "is_release": True,
        "type": "release",
        "platform": "beatport",
        "id": str(rel_raw.get("id") or ""),
        "title": rel_name,
        "artist": rel_artist,
        "label": label_name,
        "catalog": catalog,
        "release_date": pub_date,
        "cover_url": cover_uri or None,
        "tracks": [],
    }

    # Ищем треклист релиза в dehydratedState React Query
    dehydrated = props.get("dehydratedState", {})
    for q in dehydrated.get("queries", []):
        qk = q.get("queryKey", [])
        if qk and qk[0] == "tracks":
            raw_tracks = q.get("state", {}).get("data", {}).get("results", [])
            for t in raw_tracks:
                if isinstance(t, dict):
                    t_meta = parse_beatport_track_data(t, release=rel_raw, default_cover=cover_uri)
                    release_meta["tracks"].append(t_meta)

    # Если в dehydratedState не найдено, парсим через треки в HTML
    if not release_meta["tracks"]:
        track_links = soup.find_all("a", href=re.compile(r"/track/[^/]+/(\d+)"))
        seen_tids = set()
        for tl in track_links:
            m = re.search(r"/track/[^/]+/(\d+)", tl["href"])
            if m:
                tid = m.group(1)
                if tid in seen_tids:
                    continue
                seen_tids.add(tid)
                name_clean = (tl.get_text() or "").strip()
                if name_clean and len(name_clean) > 1:
                    release_meta["tracks"].append({
                        "is_release": False,
                        "platform": "beatport",
                        "track_id": tid,
                        "artist": rel_artist,
                        "title": name_clean,
                        "mix": None,
                        "album": rel_name,
                        "label": label_name,
                        "catalog": catalog,
                        "genre": None,
                        "bpm": None,
                        "key": None,
                        "isrc": None,
                        "release_date": pub_date,
                        "cover_url": cover_uri or None,
                        "duration_sec": None,
                        "is_exclusive": False,
                    })

    return release_meta


def parse_beatport(url: str) -> dict:
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    next_script = soup.find("script", id="__NEXT_DATA__")
    data = json.loads(next_script.string) if (next_script and next_script.string) else {}

    # 1. Проверяем, является ли это релизом / альбомом
    if "/release/" in url.lower():
        return parse_beatport_release(url, resp.text, data, soup)

    # 2. Одиночный трек
    props = data.get("props", {}).get("pageProps", {})
    track_raw = props.get("track")

    cover_url = None
    og_img = soup.find("meta", property="og:image")
    if og_img and og_img.get("content"):
        cover_url = og_img["content"].replace("{w}", "1400").replace("{h}", "1400")

    if isinstance(track_raw, dict):
        meta = parse_beatport_track_data(track_raw, default_cover=cover_url)
    else:
        m_id = re.search(r"/track/[^/]+/(\d+)", url)
        tid = m_id.group(1) if m_id else ""
        og_title = soup.find("meta", property="og:title")
        raw_t = og_title["content"] if og_title and og_title.get("content") else (soup.title.string if soup.title else "")
        art, tit = clean_beatport_title(raw_t)

        meta = {
            "is_release": False,
            "platform": "beatport",
            "track_id": tid,
            "artist": art or "Unknown Artist",
            "title": tit or "Beatport Track",
            "mix": None,
            "album": None,
            "label": None,
            "catalog": None,
            "genre": None,
            "bpm": None,
            "key": None,
            "isrc": None,
            "release_date": None,
            "cover_url": cover_url,
            "duration_sec": None,
            "is_exclusive": False,
        }

    return meta


def parse_deezer_album(aid: str) -> dict:
    r = requests.get(f"https://api.deezer.com/album/{aid}", headers=HEADERS, timeout=12)
    r.raise_for_status()
    d = r.json()

    rel_name = (d.get("title") or "Album").strip()
    artist_obj = d.get("artist") or {}
    rel_artist = (artist_obj.get("name") if isinstance(artist_obj, dict) else "Various Artists") or "Various Artists"
    cover = d.get("cover_xl") or d.get("cover_big")
    label = d.get("label")
    pub_date = d.get("release_date")

    release_meta = {
        "is_release": True,
        "type": "release",
        "platform": "deezer",
        "id": aid,
        "title": rel_name,
        "artist": rel_artist,
        "label": label,
        "catalog": None,
        "release_date": pub_date,
        "cover_url": cover,
        "tracks": [],
    }

    raw_tracks = d.get("tracks", {}).get("data", [])
    for t in raw_tracks:
        t_artist_obj = t.get("artist") or {}
        t_artist = (t_artist_obj.get("name") if isinstance(t_artist_obj, dict) else rel_artist) or rel_artist
        t_title = (t.get("title_short") or t.get("title") or "Track").strip()
        t_mix = (t.get("title_version") or "").strip("()") or None
        dur = int(t.get("duration") or 0) or None

        release_meta["tracks"].append({
            "is_release": False,
            "platform": "deezer",
            "track_id": str(t.get("id")),
            "artist": t_artist,
            "title": t_title,
            "mix": t_mix,
            "album": rel_name,
            "label": label,
            "catalog": None,
            "genre": None,
            "bpm": None,
            "key": None,
            "isrc": None,
            "release_date": pub_date,
            "cover_url": cover,
            "duration_sec": dur,
            "is_exclusive": False,
        })

    return release_meta


def parse_deezer(url: str) -> dict:
    real_url = (url or "").strip()
    if "deezer.page.link" in real_url or "link.deezer.com" in real_url:
        try:
            r_redir = requests.get(real_url, headers=HEADERS, allow_redirects=True, timeout=10)
            real_url = r_redir.url
        except Exception:
            pass

    # Проверяем, альбом ли это
    m_alb = re.search(r"/album/(\d+)", real_url)
    if m_alb:
        return parse_deezer_album(m_alb.group(1))

    m = re.search(r"/track/(\d+)", real_url)
    if not m:
        raise ValueError("Не удалось извлечь ID трека или альбома из ссылки Deezer.")
    tid = m.group(1)

    meta = {
        "is_release": False,
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
            meta["title"] = (d.get("title_short") or d.get("title") or meta["title"]).strip()
            meta["mix"] = (d.get("title_version") or "").strip("()") or None
            if isinstance(d.get("artist"), dict):
                meta["artist"] = (d["artist"].get("name") or meta["artist"]).strip()
            if isinstance(d.get("album"), dict):
                meta["album"] = (d["album"].get("title") or "").strip() or None
                meta["cover_url"] = d["album"].get("cover_xl") or d["album"].get("cover_big")
            meta["duration_sec"] = int(d.get("duration") or 0) or None
            meta["isrc"] = d.get("isrc")
            meta["bpm"] = str(int(d["bpm"])) if d.get("bpm") else None
            meta["release_date"] = d.get("release_date")
    except Exception as e:
        logger.warning(f"Deezer metadata warning: {e}")
    return meta


def parse_qobuz_album(aid: str) -> dict:
    r = requests.get(
        "https://www.qobuz.com/api.json/0.2/album/get",
        params={"album_id": aid},
        headers={"X-App-Id": "735532640", **HEADERS},
        timeout=12,
    )
    r.raise_for_status()
    d = r.json()

    rel_name = (d.get("title") or "Album").strip()
    artist_obj = d.get("artist") or {}
    rel_artist = (artist_obj.get("name") if isinstance(artist_obj, dict) else "Various Artists") or "Various Artists"
    img_obj = d.get("image") or {}
    cover = img_obj.get("large") or img_obj.get("small") if isinstance(img_obj, dict) else None
    label_obj = d.get("label") or {}
    label = label_obj.get("name") if isinstance(label_obj, dict) else None
    pub_date = d.get("release_date_original")

    release_meta = {
        "is_release": True,
        "type": "release",
        "platform": "qobuz",
        "id": aid,
        "title": rel_name,
        "artist": rel_artist,
        "label": label,
        "catalog": None,
        "release_date": pub_date,
        "cover_url": cover,
        "tracks": [],
    }

    raw_tracks = d.get("tracks", {}).get("items", [])
    for t in raw_tracks:
        perf = t.get("performer") or {}
        t_artist = (perf.get("name") if isinstance(perf, dict) else rel_artist) or rel_artist
        t_title = (t.get("title") or "Track").strip()
        t_mix = (t.get("version") or "").strip() or None
        dur = int(t.get("duration") or 0) or None

        release_meta["tracks"].append({
            "is_release": False,
            "platform": "qobuz",
            "track_id": str(t.get("id")),
            "artist": t_artist,
            "title": t_title,
            "mix": t_mix,
            "album": rel_name,
            "label": label,
            "catalog": None,
            "genre": None,
            "bpm": None,
            "key": None,
            "isrc": t.get("isrc"),
            "release_date": pub_date,
            "cover_url": cover,
            "duration_sec": dur,
            "is_exclusive": False,
        })

    return release_meta


def parse_qobuz(url: str) -> dict:
    real_url = (url or "").strip()

    # Проверяем, альбом ли это
    m_alb = re.search(r"/album/(?:[^/?#]+/)?([a-z0-9]+)", real_url, re.I)
    if m_alb:
        return parse_qobuz_album(m_alb.group(1))

    m = re.search(r"qobuz\.com/(?:[a-z]{2}-[a-z]{2}/)?track/(?:[^/?#]+/)?(\d+)", real_url, re.I)
    if not m:
        m = re.search(r"/track/(\d+)", real_url, re.I)
    if not m:
        raise ValueError("Не удалось извлечь ID трека или альбома из ссылки Qobuz.")
    tid = m.group(1)

    meta = {
        "is_release": False,
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
            meta["title"] = (d.get("title") or meta["title"]).strip()
            meta["mix"] = (d.get("version") or "").strip() or None
            perf = d.get("performer") or {}
            if isinstance(perf, dict) and perf.get("name"):
                meta["artist"] = (perf["name"] or meta["artist"]).strip()
            alb = d.get("album") or {}
            if isinstance(alb, dict):
                meta["album"] = (alb.get("title") or "").strip() or None
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
    target_format = (target_format or "mp3").lower().strip()
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
    full_title = (meta.get("title") or "Unknown Track").strip()
    mix = (meta.get("mix") or "").strip()
    if mix and mix.lower() not in full_title.lower():
        full_title = f"{full_title} ({mix})"

    artist = (meta.get("artist") or "Unknown Artist").strip()
    album = (meta.get("album") or full_title).strip()

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
    r = requests.get(full_url, headers=HEADERS, timeout=10)
    r.raise_for_status()
    return r.json()


def download_from_audiomack(meta: dict, target_format: str, tmp_dir: str, allow_radio_fallback: bool = False):
    try:
        artist = (meta.get("artist") or "").strip()
        title = (meta.get("title") or "").strip()
        mix = (meta.get("mix") or "").strip()
        expected_dur = int(meta.get("duration_sec") or 0)
        clean_artist = (artist.split(",")[0] or "").strip() if artist else ""

        queries = [
            f"{clean_artist} {title} {mix}".strip(),
            f"{clean_artist} {title} Extended",
            f"{clean_artist} {title}",
        ]
        seen_ids = set()
        radio_candidates = []

        for q in queries:
            if not q:
                continue
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

                    if req_art and req_art not in res_art and res_art not in req_art:
                        continue
                    if req_tit and req_tit not in res_tit:
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
    except ValueError:
        raise
    except Exception as e:
        logger.debug(f"Audiomack search passed: {e}")

    return None, 0


def download_from_soundcloud_search(meta: dict, target_format: str, tmp_dir: str, allow_radio_fallback: bool = False):
    artist = (meta.get("artist") or "").strip()
    title = (meta.get("title") or "").strip()
    mix = (meta.get("mix") or "").strip()
    expected_dur = int(meta.get("duration_sec") or 0)
    clean_artist = (artist.split(",")[0] or "").strip() if artist else ""

    is_bp = (meta.get("platform") == "beatport")
    queries = []

    if is_bp:
        # Для Beatport в приоритете поиск полного Extended Mix
        if mix and mix.lower() not in title.lower():
            queries.append(f"scsearch5:{clean_artist} {title} {mix}".strip())
        queries.append(f"scsearch5:{clean_artist} {title} Extended Mix".strip())
        queries.append(f"scsearch5:{clean_artist} {title}".strip())

        if mix and mix.lower() not in title.lower():
            queries.append(f"ytsearch5:{clean_artist} {title} {mix}".strip())
        queries.append(f"ytsearch5:{clean_artist} {title} Extended Mix".strip())
        queries.append(f"ytsearch5:{clean_artist} {title}".strip())
    else:
        # Для Deezer, Qobuz и других сервисов ищем оригинальную альбомную версию
        if mix and mix.lower() not in title.lower():
            queries.append(f"ytsearch5:{clean_artist} {title} {mix}".strip())
            queries.append(f"scsearch5:{clean_artist} {title} {mix}".strip())
        queries.append(f"ytsearch5:{clean_artist} {title}".strip())
        queries.append(f"scsearch5:{clean_artist} {title}".strip())

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(tmp_dir, "search_%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 20,
    }

    req_tit = normalize_for_match(title)
    req_art = normalize_for_match(clean_artist)
    radio_candidate = None

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            for query in queries:
                try:
                    info = ydl.extract_info(query, download=False)
                    entries = info.get("entries") or []
                    for entry in entries:
                        if not entry:
                            continue
                        ent_title = entry.get("title") or ""
                        ent_uploader = entry.get("uploader") or ""
                        dur = int(entry.get("duration") or 0)

                        norm_text = normalize_for_match(f"{ent_uploader} {ent_title}")
                        if req_tit and req_tit not in norm_text:
                            continue

                        # Точное совпадение по длительности для Extended Mix
                        if expected_dur > 0 and abs(dur - expected_dur) <= 25:
                            dl_info = ydl.extract_info(entry["webpage_url"], download=True)
                            dl_file = ydl.prepare_filename(dl_info)
                            return convert_audio_to_format(dl_file, target_format, tmp_dir), dur

                        if dur > 60 and not radio_candidate:
                            radio_candidate = (entry, dur)
                except Exception:
                    pass

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
        logger.warning(f"Audio search warning: {e}")

    return None, 0


def process_download_request(url: str, meta: dict, target_format: str, tmp_dir: str, allow_radio_fallback: bool = False):
    actual_dur = meta.get("duration_sec") or 0
    audio_path = None

    # Прямая ссылка на SoundCloud или Bandcamp
    if any(d in url.lower() for d in ("soundcloud.com", "bandcamp.com")) and meta.get("platform") == "soundcloud":
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
            clean_art = (meta.get("artist") or "Artist").strip()
            clean_tit = (meta.get("title") or "Track").strip()
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
        "👋 <b>Привет! Я бот для скачивания электронной музыки и DJ-треков:</b>\n\n"
        "• <b>Beatport</b> (поддержка треков и <b>полных релизов / EP</b>, BPM, тональность Key, лейбл и поиск <b>Extended Mix</b>)\n"
        "• <b>Deezer</b> и <b>Qobuz</b> (треки и альбомы)\n"
        "• <b>SoundCloud / Bandcamp</b> (по прямой ссылке)\n\n"
        "💎 <b>Никаких капч, привязок карт и подтверждений!</b>\n"
        "Просто отправьте ссылку, выберите трек или формат (<b>MP3 320 / FLAC / WAV</b>) и бот пришлёт оригинальный файл.",
        parse_mode="HTML",
    )


def build_release_card(rel_id: str, res: dict):
    rel_name = res.get("title") or "Release"
    rel_artist = res.get("artist") or "Various Artists"
    plat_name = res.get("platform", "beatport").capitalize()
    tracks = res.get("tracks") or []

    lines = [
        f"💿 <b>{plat_name} Release / EP</b>",
        f"🎵 <b>{rel_artist} – {rel_name}</b>",
    ]
    if res.get("label"):
        lbl = f"{res['label']} [{res['catalog']}]" if res.get("catalog") else res["label"]
        lines.append(f"🏷 <b>Label:</b> {lbl}")
    if res.get("release_date"):
        lines.append(f"📅 <b>Дата:</b> {res['release_date']}")
    lines.append(f"🔢 <b>Всего треков:</b> {len(tracks)}")
    lines.append("\n📋 <b>Треклист:</b>")

    for idx, trk in enumerate(tracks[:20], 1):
        mix_str = f" ({trk['mix']})" if trk.get("mix") else ""
        dur_str = f" [{format_mm_ss(trk.get('duration_sec') or 0)}]" if trk.get("duration_sec") else ""
        bpm_str = f" • {trk['bpm']} BPM" if trk.get("bpm") else ""
        key_str = f" • {trk['key']}" if trk.get("key") else ""
        lines.append(f"<b>{idx}.</b> {trk['title']}{mix_str}{dur_str}{bpm_str}{key_str}")

    if len(tracks) > 20:
        lines.append(f"<i>...и ещё {len(tracks) - 20} треков</i>")

    lines.append("\n👇 <i>Выберите трек для скачивания или скачайте весь релиз целиком:</i>")

    # Формируем клавиатуру
    kb_rows = []
    # Кнопки для отдельных треков (до 15)
    row = []
    for idx, trk in enumerate(tracks[:15]):
        short_title = trk.get("title") or f"Track {idx+1}"
        if len(short_title) > 22:
            short_title = short_title[:20] + "…"
        row.append(InlineKeyboardButton(text=f"{idx+1}. {short_title}", callback_data=f"rel_trk|{rel_id}|{idx}"))
        if len(row) == 2:
            kb_rows.append(row)
            row = []
    if row:
        kb_rows.append(row)

    # Кнопки массового скачивания всего релиза
    kb_rows.append([
        InlineKeyboardButton(text="⚡ Скачать весь EP (MP3 320)", callback_data=f"rel_all|{rel_id}|mp3"),
        InlineKeyboardButton(text="💎 Весь EP (FLAC)", callback_data=f"rel_all|{rel_id}|flac"),
    ])
    kb_rows.append([
        InlineKeyboardButton(text="💿 Скачать весь EP (WAV)", callback_data=f"rel_all|{rel_id}|wav"),
    ])

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows)


@dp.message(F.text)
async def handle_link(message: types.Message):
    text = (message.text or "").strip()
    url_match = re.search(r"(https?://\S+)", text)
    if not url_match:
        await message.answer(
            "Отправьте ссылку на трек или релиз с <b>Beatport</b>, <b>Deezer</b>, <b>Qobuz</b> или <b>SoundCloud</b>.",
            parse_mode="HTML",
        )
        return

    url = (url_match.group(1) or "").strip()
    low_url = url.lower()

    # Прямая ссылка на SoundCloud / Bandcamp
    if any(d in low_url for d in ("soundcloud.com", "bandcamp.com")):
        req_id = uuid.uuid4().hex[:10]
        _PENDING_TASKS[req_id] = {
            "type": "track",
            "url": url,
            "meta": {"platform": "soundcloud", "title": None, "artist": None},
        }
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

    status_msg = await message.answer("🔍 Читаю данные...")
    try:
        if "beatport.com" in low_url:
            parsed = await asyncio.wait_for(asyncio.to_thread(parse_beatport, url), timeout=15.0)
        elif "deezer." in low_url or "deezer.page.link" in low_url:
            parsed = await asyncio.wait_for(asyncio.to_thread(parse_deezer, url), timeout=15.0)
        elif "qobuz.com" in low_url:
            parsed = await asyncio.wait_for(asyncio.to_thread(parse_qobuz, url), timeout=15.0)
        else:
            await status_msg.edit_text(
                "⚠️ Поддерживаются ссылки на <b>Beatport</b>, <b>Deezer</b>, <b>Qobuz</b> и <b>SoundCloud</b>.",
                parse_mode="HTML",
            )
            return
    except Exception as e:
        logger.exception("Parse link error")
        await status_msg.edit_text(f"❌ Ошибка разбора ссылки: {e}")
        return

    # 1. Если это релиз / EP / альбом:
    if parsed.get("is_release"):
        rel_id = uuid.uuid4().hex[:10]
        _PENDING_TASKS[rel_id] = {
            "type": "release",
            "url": url,
            "release": parsed,
            "tracks": parsed.get("tracks") or [],
        }
        card_text, kb = build_release_card(rel_id, parsed)
        await status_msg.edit_text(card_text, reply_markup=kb, parse_mode="HTML")
        return

    # 2. Если это одиночный трек:
    meta = parsed
    clean_artist = (meta.get("artist") or "Various Artists").strip()
    clean_title = (meta.get("title") or "Track").strip()
    mix = (meta.get("mix") or "").strip()
    if mix and mix.lower() not in clean_title.lower():
        clean_title = f"{clean_title} ({mix})"

    req_id = uuid.uuid4().hex[:10]
    _PENDING_TASKS[req_id] = {"type": "track", "url": url, "meta": meta}

    plat = meta.get("platform", "beatport").capitalize()
    plat_badge = f"🎧 <b>{plat} Track</b>"
    dur_str = format_mm_ss(meta.get("duration_sec") or 0)
    excl_badge = " 🔒 <i>(Exclusive)</i>" if meta.get("is_exclusive") else ""

    lines = [
        plat_badge,
        f"🎵 <b>{clean_artist} – {clean_title}</b>{excl_badge}",
    ]
    if meta.get("label"):
        lbl_str = f"{meta['label']} [{meta['catalog']}]" if meta.get("catalog") else meta["label"]
        lines.append(f"🏷 <b>Label:</b> {lbl_str}")
    if meta.get("album"):
        lines.append(f"💿 <b>Release:</b> {meta['album']}")
    if meta.get("genre"):
        lines.append(f"🎶 <b>Genre:</b> {meta['genre']}")
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


# ==================== ВЫБОР ТРЕКА ИЗ РЕЛИЗА ====================
@dp.callback_query(F.data.startswith("rel_trk|"))
async def handle_release_track_select(call: CallbackQuery):
    parts = call.data.split("|")
    if len(parts) < 3:
        await call.answer("Устаревшая кнопка")
        return
    _, rel_id, idx_str = parts
    idx = int(idx_str)

    task = _PENDING_TASKS.get(rel_id)
    if not task or "tracks" not in task or idx >= len(task["tracks"]):
        await call.answer("Сессия релиза устарела, отправьте ссылку заново.", show_alert=True)
        return

    trk_meta = task["tracks"][idx]
    rel_meta = task["release"]
    clean_artist = (trk_meta.get("artist") or "Artist").strip()
    clean_title = (trk_meta.get("title") or "Track").strip()
    mix = (trk_meta.get("mix") or "").strip()
    if mix and mix.lower() not in clean_title.lower():
        clean_title = f"{clean_title} ({mix})"

    trk_req_id = uuid.uuid4().hex[:10]
    _PENDING_TASKS[trk_req_id] = {
        "type": "track",
        "url": task["url"],
        "meta": trk_meta,
        "parent_rel_id": rel_id,
    }

    lines = [
        f"🎧 <b>{clean_artist} – {clean_title}</b>",
        f"💿 Из релиза: <b>{rel_meta.get('title')}</b> (Трек {idx+1} из {len(task['tracks'])})",
    ]
    if trk_meta.get("label"):
        lines.append(f"🏷 <b>Label:</b> {trk_meta['label']}")
    if trk_meta.get("bpm") or trk_meta.get("key"):
        lines.append(f"⚡ <b>BPM:</b> {trk_meta.get('bpm') or '—'} | 🎹 <b>Key:</b> {trk_meta.get('key') or '—'}")
    if trk_meta.get("duration_sec"):
        lines.append(f"⏱ <b>Длительность:</b> {format_mm_ss(trk_meta['duration_sec'])}")
    lines.append("\n👇 <b>Выберите формат для скачивания:</b>")

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🎧 MP3 (320 kbps)", callback_data=f"dl|{trk_req_id}|mp3|0"),
                InlineKeyboardButton(text="💎 FLAC (Lossless)", callback_data=f"dl|{trk_req_id}|flac|0"),
                InlineKeyboardButton(text="💿 WAV", callback_data=f"dl|{trk_req_id}|wav|0"),
            ],
            [
                InlineKeyboardButton(text="« Назад к треклисту релиза", callback_data=f"rel_back|{rel_id}"),
            ],
        ]
    )
    await call.message.edit_text("\n".join(lines), reply_markup=kb, parse_mode="HTML")
    await call.answer()


@dp.callback_query(F.data.startswith("rel_back|"))
async def handle_release_back(call: CallbackQuery):
    parts = call.data.split("|")
    rel_id = parts[1]
    task = _PENDING_TASKS.get(rel_id)
    if not task or "release" not in task:
        await call.answer("Сессия устарела", show_alert=True)
        return

    card_text, kb = build_release_card(rel_id, task["release"])
    await call.message.edit_text(card_text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


# ==================== МАССОВОЕ СКАЧИВАНИЕ ВСЕГО РЕЛИЗА ====================
@dp.callback_query(F.data.startswith("rel_all|"))
async def handle_download_all_release(call: CallbackQuery):
    parts = call.data.split("|")
    if len(parts) < 3:
        await call.answer("Устаревшая кнопка")
        return
    _, rel_id, fmt = parts
    fmt = fmt.lower().strip()

    task = _PENDING_TASKS.get(rel_id)
    if not task or "tracks" not in task:
        await call.answer("Сессия релиза устарела, отправьте ссылку заново.", show_alert=True)
        return

    tracks = task["tracks"]
    total = len(tracks)
    rel_name = (task["release"].get("title") or "Release").strip()

    await call.answer(f"Начинаю скачивание релиза ({total} треков)...")
    status_msg = await call.message.answer(
        f"⏳ Начинаю скачивание релиза <b>«{rel_name}»</b> в формате <b>{fmt.upper()}</b> (0/{total})...",
        parse_mode="HTML",
    )

    success_count = 0
    for i, trk_meta in enumerate(tracks, 1):
        art = (trk_meta.get("artist") or "Artist").strip()
        tit = (trk_meta.get("title") or "Track").strip()
        mix = (trk_meta.get("mix") or "").strip()
        full_tit = f"{tit} ({mix})" if mix and mix.lower() not in tit.lower() else tit

        try:
            await status_msg.edit_text(
                f"⏳ <b>[{i}/{total}]</b> Поиск и скачивание: <b>{art} – {full_tit}</b>\n"
                f"Формат: <b>{fmt.upper()}</b>...",
                parse_mode="HTML",
            )
        except Exception:
            pass

        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                file_path, cover_path, actual_dur = await asyncio.wait_for(
                    asyncio.to_thread(process_download_request, task["url"], trk_meta, fmt, tmp_dir, True),
                    timeout=90.0,
                )
                clean_filename = f"{art} - {full_tit}.{fmt}".replace("/", "_").replace("\\", "_").replace(":", "")
                audio_file = FSInputFile(file_path, filename=clean_filename)
                thumb_file = FSInputFile(cover_path) if cover_path and os.path.exists(cover_path) else None

                caption_lines = [
                    f"🎵 <b>{art} – {full_tit}</b>",
                    f"💿 Релиз: <b>{rel_name}</b> ({i}/{total})",
                ]
                if trk_meta.get("label"):
                    caption_lines.append(f"🏷 <b>Label:</b> {trk_meta['label']}")
                if trk_meta.get("bpm") or trk_meta.get("key"):
                    caption_lines.append(f"⚡ <b>BPM:</b> {trk_meta.get('bpm') or '—'} | 🎹 <b>Key:</b> {trk_meta.get('key') or '—'}")
                if actual_dur:
                    caption_lines.append(f"⏱ <b>Length:</b> {format_mm_ss(actual_dur)} ({fmt.upper()})")
                caption_lines.append("\n🎧 <a href='https://musicvibez.org'>MusicVibez.org</a> • <a href='https://specialfordjs.org'>SpecialForDJs.org</a>")

                await call.message.answer_audio(
                    audio=audio_file,
                    title=full_tit,
                    performer=art,
                    duration=actual_dur if actual_dur and actual_dur > 0 else None,
                    thumbnail=thumb_file,
                    caption="\n".join(caption_lines),
                    parse_mode="HTML",
                )
                success_count += 1
                await asyncio.sleep(1.0)
        except Exception as err:
            logger.warning(f"Failed track {i} ({art} - {tit}): {err}")
            await call.message.answer(f"⚠️ Трек {i} (<b>{art} – {full_tit}</b>) пропущен: {str(err)[:120]}", parse_mode="HTML")

    await status_msg.edit_text(
        f"✅ <b>Готово!</b> Успешно выгружено <b>{success_count} из {total}</b> треков релиза <b>«{rel_name}»</b>.",
        parse_mode="HTML",
    )


# ==================== СКАЧИВАНИЕ ОДИНОЧНОГО ТРЕКА ====================
@dp.callback_query(F.data.startswith("dl|"))
async def handle_format_callback(call: CallbackQuery):
    parts = call.data.split("|")
    if len(parts) < 4:
        await call.answer("Устаревшая кнопка")
        return
    _, req_id, fmt, allow_radio_flag = parts
    allow_radio = allow_radio_flag == "1"

    task_data = _PENDING_TASKS.get(req_id)
    if not task_data:
        await call.answer("Сессия трека устарела, отправьте ссылку заново.", show_alert=True)
        return

    url = task_data.get("url") or ""
    meta = task_data.get("meta") or {}
    clean_artist = (meta.get("artist") or "Artist").strip()
    clean_title = (meta.get("title") or "Track").strip()
    mix = (meta.get("mix") or "").strip()
    if mix and mix.lower() not in clean_title.lower():
        clean_title = f"{clean_title} ({mix})"

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
            if meta.get("album"):
                caption_parts.append(f"💿 <b>Release:</b> {meta['album']}")
            if meta.get("genre"):
                caption_parts.append(f"🎶 <b>Genre:</b> {meta['genre']}")
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
