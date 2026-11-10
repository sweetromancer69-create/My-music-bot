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
from mutagen.flac import FLAC, Picture
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ==================== НАСТРОЙКИ ИЗ ОКРУЖЕНИЯ ====================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.environ.get("ADMIN_ID", "963491961"))
BEATPORT_USERNAME = os.environ.get("BEATPORT_USERNAME", "").strip()
BEATPORT_PASSWORD = os.environ.get("BEATPORT_PASSWORD", "").strip()
BEATPORT_TOKEN = os.environ.get("BEATPORT_TOKEN", "").strip()
# ===============================================================

if not BOT_TOKEN:
    raise RuntimeError("ОШИБКА: Переменная BOT_TOKEN не задана в окружении!")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

# Официальный OAuth2 client_id от Beatport API v4 (как в beatportdl)
BP_CLIENT_ID = "ryZ8LuyQVPqbK2mBX2Hwt4qSMtnWuTYSqBPO92yQ"
BP_BASE_URL = "https://api.beatport.com/v4"

AM_CONSUMER_KEY = "audiomack-web"
AM_CONSUMER_SECRET = "bd8a07e9f23fbe9d808646b730f89b8e"

_SC_CLIENT_ID_CACHE = {"id": "", "ts": 0}
_BP_AUTH_STATE = {
    "username": BEATPORT_USERNAME,
    "password": BEATPORT_PASSWORD,
    "access_token": BEATPORT_TOKEN,
    "refresh_token": "",
    "expires_at": 0,
}

# Кэш метаданных для кнопок выбора формата (MP3 320 / FLAC / WAV)
_PENDING_TRACKS = {}


def format_mm_ss(seconds: int) -> str:
    if seconds <= 0:
        return "—"
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"


# ==================== BEATPORT API v4 DIRECT ENGINE ====================
def bp_login_and_get_token() -> str:
    """
    Прямая авторизация в Beatport API v4 (аналог beatportdl) для скачивания оригиналов FLAC / MP3 320 напрямую с Beatport.
    """
    now = time.time()
    if _BP_AUTH_STATE["access_token"] and (_BP_AUTH_STATE["expires_at"] == 0 or now < _BP_AUTH_STATE["expires_at"] - 60):
        return _BP_AUTH_STATE["access_token"]

    # Попытка обновить по refresh_token
    if _BP_AUTH_STATE["refresh_token"]:
        try:
            r = requests.post(
                f"{BP_BASE_URL}/auth/o/token/",
                data={
                    "client_id": BP_CLIENT_ID,
                    "refresh_token": _BP_AUTH_STATE["refresh_token"],
                    "grant_type": "refresh_token",
                },
                headers=HEADERS,
                timeout=12,
            )
            if r.ok:
                d = r.json()
                _BP_AUTH_STATE["access_token"] = d.get("access_token", "")
                _BP_AUTH_STATE["refresh_token"] = d.get("refresh_token", _BP_AUTH_STATE["refresh_token"])
                _BP_AUTH_STATE["expires_at"] = now + int(d.get("expires_in", 3600))
                return _BP_AUTH_STATE["access_token"]
        except Exception as e:
            logger.warning(f"Beatport token refresh error: {e}")

    username = _BP_AUTH_STATE["username"]
    password = _BP_AUTH_STATE["password"]
    if not username or not password:
        return ""

    try:
        sess = requests.Session()
        sess.headers.update(HEADERS)
        # 1. Login -> sessionid cookie
        login_r = sess.post(
            f"{BP_BASE_URL}/auth/login/",
            json={"username": username, "password": password},
            timeout=12,
        )
        if not login_r.ok:
            logger.warning(f"Beatport login failed ({login_r.status_code}): {login_r.text[:200]}")
            return ""

        # 2. Authorize -> code
        auth_r = sess.get(
            f"{BP_BASE_URL}/auth/o/authorize/?client_id={BP_CLIENT_ID}&response_type=code",
            allow_redirects=False,
            timeout=12,
        )
        loc = auth_r.headers.get("Location", "")
        parsed = urllib.parse.urlparse(loc)
        code = urllib.parse.parse_qs(parsed.query).get("code", [""])[0]
        if not code:
            logger.warning("Beatport authorize did not return code")
            return ""

        # 3. Issue token
        tok_r = sess.post(
            f"{BP_BASE_URL}/auth/o/token/",
            data={
                "client_id": BP_CLIENT_ID,
                "grant_type": "authorization_code",
                "code": code,
            },
            timeout=12,
        )
        if tok_r.ok:
            d = tok_r.json()
            _BP_AUTH_STATE["access_token"] = d.get("access_token", "")
            _BP_AUTH_STATE["refresh_token"] = d.get("refresh_token", "")
            _BP_AUTH_STATE["expires_at"] = now + int(d.get("expires_in", 3600))
            return _BP_AUTH_STATE["access_token"]
    except Exception as e:
        logger.warning(f"Beatport direct auth error: {e}")

    return ""


def download_direct_from_beatport_api(track_id: str, target_format: str, tmp_dir: str) -> str:
    """
    Скачивает 100% оригинальный Extended Mix напрямую с серверов Beatport API v4
    (/v4/catalog/tracks/{id}/download/?quality=lossless или high), если подключен аккаунт Beatport.
    """
    token = bp_login_and_get_token()
    if not token or not track_id:
        return ""

    auth_headers = {**HEADERS, "Authorization": f"Bearer {token}"}
    bp_quality = "lossless" if target_format in ("flac", "wav") else "high"

    stream_location = ""
    for q in ([bp_quality, "high"] if bp_quality == "lossless" else ["high", "lossless"]):
        try:
            r = requests.get(
                f"{BP_BASE_URL}/catalog/tracks/{track_id}/download/?quality={q}",
                headers=auth_headers,
                timeout=12,
            )
            if r.ok:
                d = r.json()
                stream_location = d.get("location") or d.get("stream_url") or ""
                if stream_location:
                    break
        except Exception as e:
            logger.warning(f"Beatport /download/ error: {e}")

    if not stream_location:
        try:
            r = requests.get(
                f"{BP_BASE_URL}/catalog/tracks/{track_id}/stream/",
                headers=auth_headers,
                timeout=12,
            )
            if r.ok:
                d = r.json()
                stream_location = d.get("stream_url") or d.get("location") or ""
        except Exception as e:
            logger.warning(f"Beatport /stream/ error: {e}")

    if not stream_location:
        return ""

    raw_path = os.path.join(tmp_dir, "bp_direct_raw.audio")
    with requests.get(stream_location, headers=HEADERS, stream=True, timeout=30) as sr:
        sr.raise_for_status()
        with open(raw_path, "wb") as f:
            for chunk in sr.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)

    return convert_audio_to_format(raw_path, target_format, tmp_dir)


# ==================== ПАРСИНГ МЕТАДАННЫХ BEATPORT ====================
def clean_beatport_title(raw_text: str):
    cleaned = raw_text
    cleaned = re.sub(r'\|\s*Music\s*&?\s*Downloads\s*on\s*Beatport.*$', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\|\s*Beatport.*$', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s+on\s+Beatport.*$', '', cleaned, flags=re.IGNORECASE)
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
        "track_id": "",
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
        "duration_sec": 0,
        "cover_url": "",
        "is_exclusive": False,
    }
    id_match = re.search(r"/track/[^/]+/(\d+)", url)
    if id_match:
        meta["track_id"] = id_match.group(1)

    try:
        resp = requests.get(url, headers=HEADERS, timeout=12)
        if resp.ok:
            soup = BeautifulSoup(resp.text, "html.parser")
            next_script = soup.find("script", id="__NEXT_DATA__")
            if next_script and next_script.string:
                try:
                    data = json.loads(next_script.string)
                    page_props = data.get("props", {}).get("pageProps", {})
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
                        if t_info.get("track_id") or t_info.get("id"):
                            meta["track_id"] = str(t_info.get("track_id") or t_info.get("id"))
                        meta["title"] = t_info.get("track_name") or t_info.get("name") or ""
                        meta["mix"] = t_info.get("mix_name") or ""
                        artists_list = t_info.get("artists", [])
                        if isinstance(artists_list, list):
                            meta["artist"] = ", ".join(
                                [a.get("name", "") for a in artists_list if isinstance(a, dict) and a.get("name")]
                            )
                        if t_info.get("bpm"):
                            meta["bpm"] = str(t_info.get("bpm"))

                        length_ms = int(t_info.get("track_length_ms") or t_info.get("length_ms") or 0)
                        if length_ms > 0:
                            meta["duration_sec"] = round(length_ms / 1000)

                        key_val = t_info.get("key")
                        camelot_val = t_info.get("key_camelot")
                        key_str = key_val.get("name", "") if isinstance(key_val, dict) else (key_val if isinstance(key_val, str) else "")
                        meta["key"] = f"{key_str} ({camelot_val})" if (key_str and camelot_val) else (key_str or str(camelot_val or ""))

                        meta["isrc"] = t_info.get("isrc") or ""

                        price_cats = (t_info.get("price") or {}).get("category") or []
                        if isinstance(price_cats, list) and "exclusive" in price_cats:
                            meta["is_exclusive"] = True

                        genre_obj = t_info.get("genre")
                        if isinstance(genre_obj, dict):
                            meta["genre"] = genre_obj.get("name", "")

                        rel_obj = t_info.get("release") or {}
                        if isinstance(rel_obj, dict):
                            meta["catalog"] = rel_obj.get("catalog_number") or ""
                            r_date = rel_obj.get("release_date") or ""
                            meta["release_date"] = r_date.split("T")[0] if "T" in r_date else r_date
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


# ==================== КОНВЕРТАЦИЯ И ТЕГИ (MP3 320 / FLAC / WAV) ====================
def convert_audio_to_format(input_path: str, target_format: str, tmp_dir: str) -> str:
    ext = target_format.lower()
    out_path = os.path.join(tmp_dir, f"output.{ext}")
    if ext == "flac":
        cmd = ["ffmpeg", "-y", "-i", input_path, "-vn", "-ar", "44100", "-ac", "2", "-c:a", "flac", out_path]
    elif ext == "wav":
        cmd = ["ffmpeg", "-y", "-i", input_path, "-vn", "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", out_path]
    else:
        cmd = ["ffmpeg", "-y", "-i", input_path, "-vn", "-ar", "44100", "-ac", "2", "-b:a", "320k", out_path]

    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=40, check=True)
    return out_path if os.path.exists(out_path) else ""


def embed_tags(file_path: str, meta: dict, cover_bytes: bytes = None):
    full_title = meta.get("title", "Track")
    if meta.get("mix") and meta["mix"].lower() not in full_title.lower() and meta["mix"] != "Original Mix":
        full_title = f"{full_title} ({meta['mix']})"
    lbl_text = meta.get("label") or ""
    if lbl_text and meta.get("catalog"):
        lbl_text = f"{lbl_text} [{meta['catalog']}]"

    if file_path.lower().endswith(".flac"):
        try:
            audio = FLAC(file_path)
            audio["title"] = full_title
            if meta.get("artist"):
                audio["artist"] = meta["artist"]
            if lbl_text:
                audio["album"] = lbl_text
            if meta.get("bpm"):
                audio["bpm"] = str(meta["bpm"])
            if meta.get("key"):
                audio["initialkey"] = str(meta["key"])
            if meta.get("genre"):
                audio["genre"] = str(meta["genre"])
            if cover_bytes:
                pic = Picture()
                pic.type = 3
                pic.mime = "image/jpeg"
                pic.desc = "Cover"
                pic.data = cover_bytes
                audio.clear_pictures()
                audio.add_picture(pic)
            audio.save()
        except Exception as e:
            logger.warning(f"FLAC tagging warning: {e}")
        return

    if file_path.lower().endswith(".mp3"):
        try:
            try:
                tags = ID3(file_path)
            except ID3NoHeaderError:
                tags = ID3()
            tags["TIT2"] = TIT2(encoding=3, text=full_title)
            if meta.get("artist"):
                tags["TPE1"] = TPE1(encoding=3, text=meta["artist"])
            if lbl_text:
                tags["TALB"] = TALB(encoding=3, text=lbl_text)
            if meta.get("bpm"):
                tags["TBPM"] = TBPM(encoding=3, text=str(meta["bpm"]))
            if meta.get("key"):
                tags["TKEY"] = TKEY(encoding=3, text=str(meta["key"]))
            if meta.get("genre"):
                tags["TCON"] = TCON(encoding=3, text=str(meta["genre"]))
            if cover_bytes:
                tags["APIC"] = APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover_bytes)
            tags.save(file_path)
        except Exception as e:
            logger.warning(f"MP3 tagging warning: {e}")


# ==================== ПОИСК ТОЛЬКО EXTENDED MIX (БЕЗ YOUTUBE И БЕЗ RADIO EDIT) ====================
def normalize_for_match(s: str) -> str:
    s = re.sub(r"\([^\)]*\)|\[[^\]]*\]", "", (s or "").lower())
    s = re.sub(r"[^a-z0-9а-яё\s]", " ", s)
    return " ".join(s.split())


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


def download_extended_from_audiomack(meta: dict, target_format: str, tmp_dir: str, allow_radio_fallback: bool = False):
    artist = meta.get("artist", "").strip()
    title = meta.get("title", "").strip()
    mix = meta.get("mix", "").strip()
    expected_dur = int(meta.get("duration_sec") or 0)

    base_title = normalize_for_match(title)
    first_artist = normalize_for_match(artist.split(",")[0])

    queries = [f"{artist} {title} {mix}".strip(), f"{artist} {title}".strip()]
    best_radio_candidate = None

    for q in queries:
        try:
            search_res = audiomack_api_get("/search", {"q": q, "show": "songs", "limit": 8})
            for item in search_res.get("results") or []:
                item_title = normalize_for_match(str(item.get("title") or ""))
                item_artist = normalize_for_match(
                    str(item.get("artist") or (item.get("uploader") or {}).get("name") or "")
                )
                dur = int(item.get("duration") or 0)

                if base_title and base_title not in item_title and item_title not in base_title:
                    continue
                if first_artist and first_artist not in item_artist and first_artist not in item_title:
                    continue
                if dur < 60 or dur > 900:
                    continue

                # Проверяем: это полный Extended Mix (разница с Beatport <= 20 сек) или урезанный Radio Edit?
                is_extended_length = (expected_dur == 0) or (abs(dur - expected_dur) <= 20) or (dur >= expected_dur - 15)

                if not is_extended_length and not allow_radio_fallback:
                    if not best_radio_candidate:
                        best_radio_candidate = dur
                    continue

                song_id = item.get("id")
                play_data = audiomack_api_get(
                    f"/music/play/{song_id}",
                    {"environment": "desktop-web", "hq": "true", "section": "search"},
                )
                stream_url = play_data.get("signedUrl") or play_data.get("url")
                if not stream_url:
                    continue

                raw_audio_path = os.path.join(tmp_dir, "am_raw.m4a")
                with requests.get(stream_url, headers=HEADERS, stream=True, timeout=20) as sr:
                    sr.raise_for_status()
                    with open(raw_audio_path, "wb") as f:
                        for chunk in sr.iter_content(chunk_size=65536):
                            if chunk:
                                f.write(chunk)

                converted = convert_audio_to_format(raw_audio_path, target_format, tmp_dir)
                if converted:
                    return converted, dur
        except Exception as e:
            logger.warning(f"Audiomack error: {e}")

    return "", best_radio_candidate


def process_download_request(url: str, meta: dict, target_format: str, tmp_dir: str, allow_radio_fallback: bool = False):
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

    # 1. Если пользователь прислал ПРЯМУЮ ссылку на SoundCloud / Bandcamp — качаем напрямую с неё
    if any(d in url.lower() for d in ("soundcloud.com", "on.soundcloud.com", "bandcamp.com")):
        raw_tmpl = os.path.join(tmp_dir, "direct_raw.%(ext)s")
        ydl_opts = {"format": "bestaudio/best", "outtmpl": raw_tmpl, "noplaylist": True, "quiet": True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            dl_file = ydl.prepare_filename(info)
            if not meta.get("title"):
                meta["title"] = info.get("title") or "Track"
            if not meta.get("artist"):
                meta["artist"] = info.get("uploader") or "Artist"
            out_file = convert_audio_to_format(dl_file, target_format, tmp_dir)
            embed_tags(out_file, meta, cover_bytes)
            return out_file, cover_path, int(info.get("duration") or 0)

    # 2. Прямое скачивание с серверов Beatport API v4 (если задан аккаунт/токен Beatport)
    if meta.get("track_id"):
        bp_direct = download_direct_from_beatport_api(meta["track_id"], target_format, tmp_dir)
        if bp_direct and os.path.exists(bp_direct):
            embed_tags(bp_direct, meta, cover_bytes)
            return bp_direct, cover_path, int(meta.get("duration_sec") or 0)

    # 3. Поиск строго полной Extended-версии (проверка совпадения длительности с Beatport!)
    out_file, found_dur = download_extended_from_audiomack(
        meta, target_format, tmp_dir, allow_radio_fallback=allow_radio_fallback
    )
    if out_file and os.path.exists(out_file):
        embed_tags(out_file, meta, cover_bytes)
        return out_file, cover_path, found_dur

    bp_dur_str = format_mm_ss(meta.get("duration_sec") or 0)
    has_bp_creds = bool(_BP_AUTH_STATE["username"] or _BP_AUTH_STATE["access_token"])

    if found_dur and not allow_radio_fallback:
        radio_dur_str = format_mm_ss(found_dur)
        raise ValueError(
            f"EXTENDED_ONLY_ON_BEATPORT|{bp_dur_str}|{radio_dur_str}|{has_bp_creds}"
        )

    raise RuntimeError(
        f"Полная версия Extended Mix ({bp_dur_str}) доступна только напрямую с аккаунтом Beatport Streaming.\n\n"
        f"💡 Подключите аккаунт командой:\n<code>/bp_login email пароль</code>\nили\n<code>/bp_token токен</code>"
    )


# ==================== КОМАНДЫ И ОБРАБОТЧИКИ TELEGRAM ====================
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    bp_status = "✅ Подключен (прямое скачивание Extended Mix с Beatport API v4)" if (
        _BP_AUTH_STATE["username"] or _BP_AUTH_STATE["access_token"]
    ) else "⚠️ Не подключен (используется проверка длины Extended Mix)"

    await message.answer(
        "🎧 <b>Musicvibez.org Beatport Downloader (PRO Edition)</b>\n\n"
        "Отправьте ссылку на трек:\n"
        "• <b>Beatport</b> — выбор формата: <b>MP3 320 kbps</b>, <b>FLAC</b> или <b>WAV</b> (строгий контроль <b>Extended Mix</b> по длительности, без YouTube и без урезанных Radio Edit!)\n"
        "• <b>SoundCloud / Bandcamp</b> — скачивание только по вашей прямой ссылке.\n\n"
        f"🔌 <b>Прямой доступ Beatport API v4:</b> {bp_status}\n"
        "Для привязки аккаунта Beatport Streaming используйте:\n"
        "<code>/bp_login email пароль</code> или <code>/bp_token токен</code>",
        parse_mode="HTML",
    )


@dp.message(Command("bp_login"))
async def cmd_bp_login(message: types.Message):
    parts = (message.text or "").strip().split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("Использование: <code>/bp_login ваш_email ваш_пароль</code>", parse_mode="HTML")
        return
    _BP_AUTH_STATE["username"] = parts[1].strip()
    _BP_AUTH_STATE["password"] = parts[2].strip()
    _BP_AUTH_STATE["access_token"] = ""
    _BP_AUTH_STATE["refresh_token"] = ""
    status = await message.answer("🔐 Проверяю авторизацию на сервере api.beatport.com/v4...")
    token = await asyncio.to_thread(bp_login_and_get_token)
    if token:
        await status.edit_text("✅ <b>Аккаунт Beatport успешно подключен!</b>\nТеперь все треки качаются напрямую с серверов Beatport в FLAC / WAV / 320 kbps.", parse_mode="HTML")
    else:
        await status.edit_text("❌ Не удалось войти в Beatport. Проверьте логин/пароль или наличие подписки Beatport Streaming.")


@dp.message(Command("bp_token"))
async def cmd_bp_token(message: types.Message):
    parts = (message.text or "").strip().split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: <code>/bp_token ваш_bearer_токен_beatport</code>", parse_mode="HTML")
        return
    _BP_AUTH_STATE["access_token"] = parts[1].strip()
    _BP_AUTH_STATE["expires_at"] = 0
    await message.answer("✅ <b>Bearer-токен Beatport сохранен!</b> Теперь бот будет запрашивать прямые потоки с api.beatport.com/v4.", parse_mode="HTML")


@dp.message(F.text)
async def handle_link(message: types.Message):
    text = (message.text or "").strip()
    if text.startswith("/"):
        return

    url_match = re.search(r"(https?://\S+)", text)
    if not url_match:
        await message.answer("Отправьте ссылку на трек с Beatport или прямую ссылку на SoundCloud.")
        return

    url = url_match.group(1)

    # Если это прямая ссылка на SoundCloud — сразу спрашиваем формат и качаем с SoundCloud
    if "soundcloud.com" in url.lower():
        req_id = uuid.uuid4().hex[:10]
        _PENDING_TRACKS[req_id] = {
            "url": url,
            "meta": {"title": "", "artist": "", "mix": "", "duration_sec": 0},
        }
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🎧 MP3 320k", callback_data=f"dl|{req_id}|mp3|0"),
                    InlineKeyboardButton(text="💎 FLAC", callback_data=f"dl|{req_id}|flac|0"),
                    InlineKeyboardButton(text="💿 WAV", callback_data=f"dl|{req_id}|wav|0"),
                ]
            ]
        )
        await message.answer("☁️ <b>Прямая ссылка SoundCloud</b>\nВыберите формат для скачивания:", reply_markup=kb, parse_mode="HTML")
        return

    if "beatport.com" not in url.lower():
        await message.answer("⚠️ Поддерживаются ссылки на <b>Beatport</b> (и прямые ссылки на <b>SoundCloud</b>).", parse_mode="HTML")
        return

    status_msg = await message.answer("🔍 Читаю данные трека с Beatport...")
    meta = await asyncio.wait_for(asyncio.to_thread(parse_beatport, url), timeout=15.0)

    clean_artist = meta.get("artist") or "Various Artists"
    clean_title = meta.get("title") or "Track"
    if meta.get("mix") and meta["mix"].lower() not in clean_title.lower():
        clean_title = f"{clean_title} ({meta['mix']})"

    req_id = uuid.uuid4().hex[:10]
    _PENDING_TRACKS[req_id] = {"url": url, "meta": meta}

    dur_str = format_mm_ss(meta.get("duration_sec") or 0)
    excl_badge = " 🔒 <i>(Beatport Exclusive)</i>" if meta.get("is_exclusive") else ""

    lines = [f"🎵 <b>{clean_artist} – {clean_title}</b>{excl_badge}"]
    if meta.get("label"):
        lbl_str = f"{meta['label']} [{meta['catalog']}]" if meta.get("catalog") else meta["label"]
        lines.append(f"🏷 <b>Label:</b> {lbl_str}")
    if meta.get("genre"):
        lines.append(f"💿 <b>Genre:</b> {meta['genre']}")
    if meta.get("bpm") or meta.get("key"):
        lines.append(f"⚡ <b>BPM:</b> {meta.get('bpm') or '—'} | 🎹 <b>Key:</b> {meta.get('key') or '—'}")
    lines.append(f"⏱ <b>Длительность (Extended):</b> {dur_str}")
    lines.append("\n👇 <b>Выберите формат для скачивания:</b>")

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
        f"Формат: <b>{fmt.upper()}</b> | Проверка полной Extended-версии...",
        parse_mode="HTML",
    )

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            file_path, cover_path, actual_dur = await asyncio.wait_for(
                asyncio.to_thread(process_download_request, url, meta, fmt, tmp_dir, allow_radio),
                timeout=55.0,
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
            caption_parts.append("\n🎧 <a href='https://musicvibez.org'>MusicVibez.org</a> • <a href='https://specialfordjs.org'>SpecialForDJs.org</a>")

            ext = fmt.lower()
            clean_filename = f"{clean_artist} - {clean_title}.{ext}".replace("/", "_").replace("\\", "_").replace(":", "")
            audio_file = FSInputFile(file_path, filename=clean_filename)
            thumb_file = FSInputFile(cover_path) if cover_path and os.path.exists(cover_path) else None

            await call.message.edit_text(f"🚀 Выгружаю <b>{fmt.upper()}</b> в Telegram...", parse_mode="HTML")
            await call.message.answer_audio(
                audio=audio_file,
                title=clean_title,
                performer=clean_artist,
                duration=actual_dur if actual_dur > 0 else None,
                thumbnail=thumb_file,
                caption="\n".join(caption_parts),
                parse_mode="HTML",
            )
            await call.message.delete()

    except ValueError as ve:
        msg_str = str(ve)
        if msg_str.startswith("EXTENDED_ONLY_ON_BEATPORT|"):
            _, bp_dur, radio_dur, has_creds = msg_str.split("|")
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=f"⬇️ Скачать Radio Edit ({radio_dur}) в {fmt.upper()}",
                            callback_data=f"dl|{req_id}|{fmt}|1",
                        )
                    ]
                ]
            )
            await call.message.edit_text(
                f"⚠️ <b>Внимание: фильтрация Radio Edit!</b>\n\n"
                f"Трек <b>{clean_artist} – {clean_title}</b> на Beatport имеет длительность <b>{bp_dur} (Extended Mix)</b>, "
                f"а лейбл выложил в открытые стриминги только урезанную <b>Radio Edit ({radio_dur})</b>.\n\n"
                f"Бот заблокировал скачивание урезанной версии, чтобы не подменять Extended Mix.\n\n"
                f"🔑 <i>Чтобы качать закрытые Extended Mix напрямую с серверов Beatport, подключите подписку Beatport Streaming командой</i> <code>/bp_login email пароль</code>.",
                reply_markup=kb,
                parse_mode="HTML",
            )
        else:
            await call.message.edit_text(f"❌ {msg_str}", parse_mode="HTML")
    except Exception as e:
        logger.exception("Download callback error")
        await call.message.edit_text(f"❌ {str(e)[:350]}", parse_mode="HTML")


async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот успешно запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
