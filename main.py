import os
import re
import json
import time
import uuid
import hmac
import base64
import shutil
import secrets
import hashlib
import asyncio
import logging
import tempfile
import subprocess
import urllib.parse
from datetime import datetime, timedelta, timezone

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

# Хранилище метаданных треков для кнопок выбора формата (req_id -> dict)
_PENDING_TRACKS = {}
# Последний ожидающий запрос пользователя (chat_id -> {"req_id": ..., "fmt": ..., "allow_radio": ...})
# чтобы после вставки ссылки активации spotiflac:// бот автоматически докачал трек!
_WAITING_FOR_GRANT = {}

# ==================== MULTI-PLATFORM LOSSLESS GATEWAY (ZARZ V2) ====================
# Поддерживает: Beatport (FLAC Lossless Extended Mix), Tidal (Hi-Res/FLAC), Qobuz (Hi-Res/FLAC),
# Amazon Music (Ultra HD FLAC), Deezer (FLAC) без платных подписок!
ZARZ_SESSION_FILE = "/tmp/zarz_v2_session.json"


class ZarzVerificationRequired(Exception):
    def __init__(self, auth_url: str, challenge_id: str = ""):
        super().__init__("Требуется однократная активация Lossless-сервера (1 клик)")
        self.auth_url = auth_url
        self.challenge_id = challenge_id


class ZarzV2Client:
    BASE_URL = "https://api.zarz.moe/v2"
    PLATFORM = "extension"
    APP_VERSION = "beatport@1.0.0"

    def __init__(self):
        self.http = requests.Session()
        self.install_id = ""
        self.session_id = ""
        self.session_secret = ""
        self.expires_at = ""
        self.refresh_after = ""
        self.pending_challenge_id = ""
        self.pending_auth_url = ""
        self._load()

    def _load(self):
        raw_env = os.getenv("ZARZ_SESSION_JSON", "").strip()
        data = {}
        if os.path.exists(ZARZ_SESSION_FILE):
            try:
                with open(ZARZ_SESSION_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        elif raw_env:
            try:
                data = json.loads(raw_env)
            except Exception:
                data = {}
        self.install_id = data.get("install_id") or secrets.token_hex(16)
        self.session_id = data.get("session_id") or ""
        self.session_secret = data.get("session_secret") or ""
        self.expires_at = data.get("expires_at") or ""
        self.refresh_after = data.get("refresh_after") or ""
        self._save()

    def _save(self):
        try:
            with open(ZARZ_SESSION_FILE, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "install_id": self.install_id,
                        "session_id": self.session_id,
                        "session_secret": self.session_secret,
                        "expires_at": self.expires_at,
                        "refresh_after": self.refresh_after,
                    },
                    f,
                )
        except Exception:
            pass

    def clear(self):
        self.session_id = ""
        self.session_secret = ""
        self.expires_at = ""
        self.refresh_after = ""
        self._save()

    @staticmethod
    def _parse_time(val: str):
        if not val:
            return None
        try:
            dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None

    def is_valid(self) -> bool:
        if not self.session_id or not self.session_secret:
            return False
        exp = self._parse_time(self.expires_at)
        if not exp:
            return False
        return exp > datetime.now(timezone.utc) + timedelta(minutes=2)

    def bootstrap_challenge_url(self) -> str:
        resp = self.http.get(
            f"{self.BASE_URL}/bootstrap",
            params={"install_id": self.install_id, "app_version": self.APP_VERSION},
            headers={
                "Accept": "application/json",
                "User-Agent": f"SpotiFLAC-Mobile/{self.APP_VERSION}",
            },
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        if all(data.get(k) for k in ("session_id", "session_secret", "expires_at")):
            self.session_id = str(data["session_id"])
            self.session_secret = str(data["session_secret"])
            self.expires_at = str(data["expires_at"])
            self.refresh_after = str(data.get("refresh_after") or "")
            self.pending_auth_url = ""
            self.pending_challenge_id = ""
            self._save()
            return ""
        cid = str(data.get("challenge_id") or "")
        callback = "spotiflac://session-grant?" + urllib.parse.urlencode(
            {"cb_version": "v2grant", "state": "zarz-v2"}
        )
        auth_url = f"{self.BASE_URL}/challenge?" + urllib.parse.urlencode({"id": cid, "cb": callback})
        self.pending_challenge_id = cid
        self.pending_auth_url = auth_url
        return auth_url

    def exchange_grant(self, raw_grant_or_url: str) -> bool:
        text = (raw_grant_or_url or "").strip()
        grant = text
        if "grant=" in text:
            m = re.search(r"[?&]grant=([^&#\s]+)", text)
            if m:
                grant = urllib.parse.unquote(m.group(1))
        grant = grant.strip()
        if not grant:
            raise ValueError("Не удалось извлечь grant-токен из ссылки")

        resp = self.http.post(
            f"{self.BASE_URL}/session/exchange",
            json={
                "grant": grant,
                "install_id": self.install_id,
                "app_version": self.APP_VERSION,
                "platform": self.PLATFORM,
            },
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": f"SpotiFLAC-Mobile/{self.APP_VERSION}",
            },
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        if not all(data.get(k) for k in ("session_id", "session_secret", "expires_at")):
            raise RuntimeError(f"Неполный ответ сервера при обмене токена: {data}")
        self.session_id = str(data["session_id"])
        self.session_secret = str(data["session_secret"])
        self.expires_at = str(data["expires_at"])
        self.refresh_after = str(data.get("refresh_after") or "")
        self.pending_auth_url = ""
        self.pending_challenge_id = ""
        self._save()
        logger.info(f"ZarzV2 session activated until {self.expires_at}")
        return True

    def _sign_headers(self, method: str, path: str, body_bytes: bytes, app_version: str = None) -> dict:
        app_ver = app_version or self.APP_VERSION
        now = datetime.now(timezone.utc)
        ts = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
        nonce = secrets.token_hex(12)
        body_hash = hashlib.sha256(body_bytes).hexdigest()
        window = int(time.time() // 300)

        rk_bytes = hmac.new(
            self.session_secret.encode("utf-8"),
            f"{window}:{self.session_id}".encode("utf-8"),
            hashlib.sha256,
        ).digest()
        rk_str = base64.urlsafe_b64encode(rk_bytes).rstrip(b"=").decode("utf-8")

        full_url = f"{self.BASE_URL}/{path.lstrip('/')}"
        parsed_path = urllib.parse.urlparse(full_url).path

        signing_input = "\n".join(
            [
                "ZARZ-HMAC-V1",
                method.upper(),
                parsed_path,
                "",
                body_hash,
                ts,
                nonce,
                self.session_id,
                app_ver,
                self.PLATFORM,
            ]
        )
        sig = (
            base64.urlsafe_b64encode(
                hmac.new(
                    rk_str.encode("utf-8"),
                    signing_input.encode("utf-8"),
                    hashlib.sha256,
                ).digest()
            )
            .rstrip(b"=")
            .decode("utf-8")
        )
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": f"SpotiFLAC-Mobile/{app_ver}",
            "X-Zarz-Session": self.session_id,
            "X-Zarz-Timestamp": ts,
            "X-Zarz-Nonce": nonce,
            "X-Zarz-Body-Sha256": body_hash,
            "X-Zarz-Signature": sig,
            "X-Zarz-App-Version": app_ver,
            "X-Zarz-Platform": self.PLATFORM,
        }

    def ensure_session(self):
        if not self.is_valid():
            auth_url = self.bootstrap_challenge_url()
            if auth_url:
                raise ZarzVerificationRequired(auth_url, self.pending_challenge_id)
        # Проверяем необходимость продления сессии
        exp = self._parse_time(self.expires_at)
        ref = self._parse_time(self.refresh_after)
        now = datetime.now(timezone.utc)
        if (ref and now >= ref) or (exp and (exp - now) <= timedelta(hours=1)):
            try:
                body_bytes = json.dumps({"install_id": self.install_id}, separators=(",", ":")).encode("utf-8")
                headers = self._sign_headers("POST", "/session/refresh", body_bytes)
                r = self.http.post(f"{self.BASE_URL}/session/refresh", data=body_bytes, headers=headers, timeout=15)
                if r.status_code == 200:
                    d = r.json()
                    if d.get("session_id") and d.get("session_secret"):
                        self.session_id = str(d["session_id"])
                        self.session_secret = str(d["session_secret"])
                        self.expires_at = str(d.get("expires_at") or self.expires_at)
                        self.refresh_after = str(d.get("refresh_after") or self.refresh_after)
                        self._save()
            except Exception as e:
                logger.warning(f"Session refresh warning: {e}")

    def signed_post(self, path: str, payload: dict, extra_headers: dict = None, app_version: str = None) -> dict:
        self.ensure_session()
        body_bytes = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = self._sign_headers("POST", path, body_bytes, app_version=app_version)
        if extra_headers:
            headers.update(extra_headers)
        url = f"{self.BASE_URL}/{path.lstrip('/')}"
        resp = self.http.post(url, data=body_bytes, headers=headers, timeout=35)
        if resp.status_code in (401, 428):
            try:
                err_data = resp.json()
            except Exception:
                err_data = {}
            code = str(err_data.get("code") or "").upper()
            if "SESSION" in code or "AUTH" in code or "VERIFY" in code or resp.status_code == 428:
                self.clear()
                auth_url = self.bootstrap_challenge_url()
                if auth_url:
                    raise ZarzVerificationRequired(auth_url, self.pending_challenge_id)
        if resp.status_code != 200:
            try:
                err_json = resp.json()
                err_msg = err_json.get("error") or err_json.get("detail") or err_json.get("message") or resp.text[:200]
            except Exception:
                err_msg = resp.text[:200]
            raise RuntimeError(f"Ошибка Lossless-шлюза ({resp.status_code}): {err_msg}")
        return resp.json()

    def get_ticket(self, provider: str, resource_str: str, app_version: str = None) -> str:
        res_hash = hashlib.sha256(resource_str.encode("utf-8")).hexdigest()
        data = self.signed_post(
            "/tickets",
            {
                "capability": "download_ticket",
                "provider": provider,
                "resource_hash": res_hash,
            },
            app_version=app_version,
        )
        tid = str(data.get("ticket_id") or data.get("ticket") or "").strip()
        if not tid:
            raise RuntimeError(f"Не получен download_ticket для {provider}")
        return tid

    # 1. BEATPORT DIRECT LOSSLESS (Extended Mix FLAC / AAC 256)
    def download_beatport_track(self, track_id: str, target_format: str, tmp_dir: str) -> str:
        tid = str(track_id).strip()
        quality = "lossless"
        ticket_id = self.get_ticket("btp", f"beatport:track:{tid}", app_version="beatport@1.0.0")
        res = self.signed_post(
            "/dl/btp",
            {"id": tid, "quality": quality, "allow_fallback": False},
            extra_headers={"X-Zarz-Ticket": ticket_id},
            app_version="beatport@1.0.0",
        )
        dl_url = str(res.get("download_url") or "").strip()
        if not dl_url:
            raise RuntimeError("Сервер Beatport не вернул download_url")
        ext = str(res.get("format") or "flac").lower()
        raw_path = os.path.join(tmp_dir, f"bp_zarz_{tid}.{ext}")
        with self.http.get(dl_url, headers={"User-Agent": "SpotiFLAC-Mobile/beatport@1.0.0"}, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(raw_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=131072):
                    if chunk:
                        f.write(chunk)
        return convert_audio_to_format(raw_path, target_format, tmp_dir)

    # 2. TIDAL LOSSLESS / HI-RES FLAC
    def download_tidal_track(self, track_id: str, target_format: str, tmp_dir: str) -> str:
        tid = str(track_id).strip()
        for q_tier in ("HI_RES_LOSSLESS", "LOSSLESS"):
            try:
                ticket_id = self.get_ticket("tid", f"tid:track:{tid.lower()}", app_version="tidal-web@1.2.5")
                payload = self.signed_post(
                    "/dl/tid",
                    {"id": tid, "quality": q_tier},
                    extra_headers={"X-Zarz-Ticket": ticket_id},
                    app_version="tidal-web@1.2.5",
                )
                data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
                manifest_b64 = data.get("manifest") if isinstance(data, dict) else None
                if not manifest_b64:
                    continue
                manifest_text = base64.b64decode(manifest_b64).decode("utf-8", errors="ignore").strip()
                if manifest_text.startswith("{"):
                    bts = json.loads(manifest_text)
                    urls = bts.get("urls") or []
                    if not urls:
                        continue
                    raw_path = os.path.join(tmp_dir, f"tidal_{tid}.flac")
                    with self.http.get(urls[0], headers=HEADERS, stream=True, timeout=60) as r:
                        r.raise_for_status()
                        with open(raw_path, "wb") as f:
                            for chunk in r.iter_content(chunk_size=131072):
                                if chunk:
                                    f.write(chunk)
                    return convert_audio_to_format(raw_path, target_format, tmp_dir)
                else:
                    # DASH MPD manifest (Hi-Res 24-bit FLAC insegmented MP4)
                    init_m = re.search(r'initialization="([^"]+)"', manifest_text, re.I)
                    media_m = re.search(r'media="([^"]+)"', manifest_text, re.I)
                    if not init_m or not media_m:
                        continue
                    init_url = init_m.group(1).replace("&amp;", "&")
                    media_tpl = media_m.group(1).replace("&amp;", "&")
                    seg_count = 0
                    for sm in re.finditer(r'<S\s+[^>]*d="(\d+)"(?:\s+r="(-?\d+)")?', manifest_text, re.I):
                        rep = int(sm.group(2) or "0")
                        seg_count += max(0, rep) + 1
                    if seg_count <= 0:
                        continue
                    raw_m4a = os.path.join(tmp_dir, f"tidal_dash_{tid}.m4a")
                    with open(raw_m4a, "wb") as out_f:
                        r_init = self.http.get(init_url, headers=HEADERS, timeout=25)
                        r_init.raise_for_status()
                        out_f.write(r_init.content)
                        for idx in range(1, seg_count + 1):
                            seg_url = media_tpl.replace("$Number$", str(idx))
                            r_seg = self.http.get(seg_url, headers=HEADERS, timeout=25)
                            r_seg.raise_for_status()
                            out_f.write(r_seg.content)
                    return convert_audio_to_format(raw_m4a, target_format, tmp_dir)
            except ZarzVerificationRequired:
                raise
            except Exception as e:
                logger.warning(f"Tidal tier {q_tier} warning for {tid}: {e}")
        raise RuntimeError("Не удалось скачать трек с Tidal")

    # 3. QOBUZ HI-RES / LOSSLESS FLAC
    def download_qobuz_track(self, track_id: str, target_format: str, tmp_dir: str) -> str:
        tid = str(track_id).strip()
        track_url = f"https://open.qobuz.com/track/{tid}"
        for q_code in ("hi-res-max", "hi-res", "cd"):
            try:
                ticket_id = self.get_ticket("qbz", f"qbz:track:{track_url.lower()}", app_version="qobuz-web@1.1.0")
                res = self.signed_post(
                    "/dl/qbz",
                    {
                        "quality": q_code,
                        "upload_to_r2": False,
                        "id": tid,
                        "type": "track",
                        "url": track_url,
                    },
                    extra_headers={"X-Zarz-Ticket": ticket_id},
                    app_version="qobuz-web@1.1.0",
                )
                nested = res.get("data") if isinstance(res.get("data"), dict) else {}
                dl_url = (
                    res.get("download_url")
                    or res.get("url")
                    or res.get("link")
                    or nested.get("download_url")
                    or nested.get("url")
                    or nested.get("link")
                )
                if not dl_url:
                    continue
                raw_path = os.path.join(tmp_dir, f"qobuz_{tid}.flac")
                with self.http.get(str(dl_url), headers=HEADERS, stream=True, timeout=60) as r:
                    r.raise_for_status()
                    with open(raw_path, "wb") as f:
                        for chunk in r.iter_content(chunk_size=131072):
                            if chunk:
                                f.write(chunk)
                return convert_audio_to_format(raw_path, target_format, tmp_dir)
            except ZarzVerificationRequired:
                raise
            except Exception as e:
                logger.warning(f"Qobuz tier {q_code} warning for {tid}: {e}")
        raise RuntimeError("Не удалось скачать трек с Qobuz")

    # 4. AMAZON MUSIC ULTRA HD / HD FLAC
    def download_amazon_track(self, asin: str, target_format: str, tmp_dir: str) -> tuple:
        clean_asin = str(asin).strip().upper()
        ticket_id = self.get_ticket(
            "amazeamazeamaze",
            f"amazeamazeamaze:track:{clean_asin.lower()}",
            app_version="amzn@2.3.12",
        )
        data = self.signed_post(
            "/dl/amazeamazeamaze",
            {"asin": clean_asin, "codec": "flac"},
            extra_headers={"X-Zarz-Ticket": ticket_id},
            app_version="amzn@2.3.12",
        )
        if isinstance(data, list) and data:
            data = data[0]
        if not isinstance(data, dict) or not data.get("audio") or not data["audio"].get("url"):
            raise RuntimeError("Сервер Amazon Music не вернул аудио-поток")

        stream_url = str(data["audio"]["url"]).strip()
        dec_key = str(data["audio"].get("key") or "").strip()
        amz_meta = data.get("meta") or {}
        cover_tpl = str(data.get("cover") or "")
        cover_url = (
            cover_tpl.replace("{size}", "1400").replace("{jpegQuality}", "95").replace("{format}", "jpg")
            if cover_tpl
            else ""
        )

        ext = "m4a" if dec_key else "flac"
        raw_path = os.path.join(tmp_dir, f"amazon_{clean_asin}.{ext}")
        with self.http.get(stream_url, headers=HEADERS, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(raw_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=131072):
                    if chunk:
                        f.write(chunk)

        if dec_key and shutil.which("ffmpeg"):
            decrypted_flac = os.path.join(tmp_dir, f"amazon_dec_{clean_asin}.flac")
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-decryption_key",
                    dec_key,
                    "-i",
                    raw_path,
                    "-c:a",
                    "flac",
                    decrypted_flac,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
            )
            if os.path.exists(decrypted_flac) and os.path.getsize(decrypted_flac) > 50_000:
                raw_path = decrypted_flac

        extra_meta = {
            "title": amz_meta.get("title") or "",
            "artist": amz_meta.get("artist") or amz_meta.get("albumArtist") or "",
            "album": amz_meta.get("album") or "",
            "isrc": amz_meta.get("isrc") or "",
            "genre": amz_meta.get("genre") or "",
            "label": amz_meta.get("label") or "",
            "release_date": amz_meta.get("date") or amz_meta.get("releaseDate") or "",
            "cover_url": cover_url,
        }
        return convert_audio_to_format(raw_path, target_format, tmp_dir), extra_meta

    # 5. DEEZER LOSSLESS FLAC
    def download_deezer_track(self, track_id: str, target_format: str, tmp_dir: str) -> str:
        tid = str(track_id).strip()
        track_url = f"https://www.deezer.com/track/{tid}"
        ticket_id = self.get_ticket("dzr", f"dzr:track:{track_url.lower()}", app_version="deezer@1.3.5")
        desc = self.signed_post(
            "/dl/dzr",
            {"id": tid, "type": "track", "platform": "deezer", "url": track_url},
            extra_headers={"X-Zarz-Ticket": ticket_id},
            app_version="deezer@1.3.5",
        )
        dl_url = str(desc.get("direct_download_url") or desc.get("download_url") or "").strip()
        if not dl_url:
            raise RuntimeError("Deezer не вернул ссылку на скачивание")
        raw_path = os.path.join(tmp_dir, f"deezer_{tid}.flac")
        with self.http.get(dl_url, headers=HEADERS, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(raw_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=131072):
                    if chunk:
                        f.write(chunk)
        return convert_audio_to_format(raw_path, target_format, tmp_dir)


ZARZ_CLIENT = ZarzV2Client()

# ==================== BEATPORT PERSONAL ACCOUNT FALLBACK (API v4) ====================
BEATPORT_CLIENT_ID = "ryZ8LuyQVPqbK2mBX2Hwt4qSMtnWuTYSqBPO92yQ"
BEATPORT_API_BASE = "https://api.beatport.com/v4"
BEATPORT_REDIRECT_URI = f"{BEATPORT_API_BASE}/auth/o/post-message/"
BP_CREDS_FILE = "/tmp/bp_credentials.json"
_BP_TOKEN_CACHE = {"access_token": os.getenv("BEATPORT_TOKEN", "").strip(), "expires_at": 0}


def format_mm_ss(seconds: int) -> str:
    if not seconds or seconds <= 0:
        return "—"
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"


def bp_login_and_get_token() -> str:
    now = time.time()
    if _BP_TOKEN_CACHE.get("access_token") and _BP_TOKEN_CACHE.get("expires_at", 0) > now + 60:
        return _BP_TOKEN_CACHE["access_token"]

    username = os.getenv("BEATPORT_USERNAME", "").strip()
    password = os.getenv("BEATPORT_PASSWORD", "").strip()
    saved_token = os.getenv("BEATPORT_TOKEN", "").strip()

    if os.path.exists(BP_CREDS_FILE):
        try:
            with open(BP_CREDS_FILE, "r", encoding="utf-8") as f:
                creds = json.load(f)
                username = creds.get("username") or username
                password = creds.get("password") or password
                saved_token = creds.get("access_token") or saved_token
        except Exception:
            pass

    if saved_token and not username:
        return saved_token
    if not username or not password:
        return ""

    s = requests.Session()
    s.headers.update(HEADERS)
    r_login = s.post(
        f"{BEATPORT_API_BASE}/auth/login/",
        json={"username": username, "password": password},
        timeout=15,
    )
    if r_login.status_code != 200:
        raise ValueError(f"Ошибка входа в Beatport ({r_login.status_code})")

    r_auth = s.get(
        f"{BEATPORT_API_BASE}/auth/o/authorize/",
        params={
            "response_type": "code",
            "client_id": BEATPORT_CLIENT_ID,
            "redirect_uri": BEATPORT_REDIRECT_URI,
        },
        allow_redirects=False,
        timeout=15,
    )
    loc = r_auth.headers.get("Location", "")
    parsed_q = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
    code = parsed_q.get("code", [""])[0]
    if not code:
        raise ValueError("Не удалось получить OAuth code от Beatport API v4.")

    r_tok = s.post(
        f"{BEATPORT_API_BASE}/auth/o/token/",
        data={
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": BEATPORT_REDIRECT_URI,
            "client_id": BEATPORT_CLIENT_ID,
        },
        timeout=15,
    )
    tok_data = r_tok.json()
    access_token = tok_data.get("access_token", "")
    expires_in = int(tok_data.get("expires_in", 3600))
    _BP_TOKEN_CACHE["access_token"] = access_token
    _BP_TOKEN_CACHE["expires_at"] = now + expires_in
    return access_token


def download_direct_from_beatport_api(track_id: str, target_format: str, tmp_dir: str) -> str:
    token = bp_login_and_get_token()
    if not token:
        return None
    auth_headers = {**HEADERS, "Authorization": f"Bearer {token}"}
    bp_quality = "lossless" if target_format in ("flac", "wav") else "high"
    for q in ([bp_quality, "high"] if bp_quality == "lossless" else ["high", "lossless"]):
        r = requests.get(
            f"{BEATPORT_API_BASE}/catalog/tracks/{track_id}/download/",
            params={"quality": q},
            headers=auth_headers,
            timeout=20,
        )
        if r.status_code == 200:
            dl_url = r.json().get("location") or r.json().get("download_url")
            if dl_url:
                ext = "flac" if q == "lossless" else "mp3"
                raw_path = os.path.join(tmp_dir, f"bp_direct_{track_id}.{ext}")
                with requests.get(dl_url, headers=HEADERS, stream=True, timeout=45) as sr:
                    sr.raise_for_status()
                    with open(raw_path, "wb") as f:
                        for chunk in sr.iter_content(chunk_size=131072):
                            if chunk:
                                f.write(chunk)
                return convert_audio_to_format(raw_path, target_format, tmp_dir)
    return None


# ==================== ПАРСИНГ МЕТАДАННЫХ С BEATPORT / TIDAL / QOBUZ / AMAZON / DEEZER ====================
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


def parse_tidal_url(url: str) -> dict:
    m = re.search(r"tidal\.com/(?:browse/)?track/(\d+)", url, re.I)
    if not m:
        raise ValueError("Не удалось извлечь ID трека из ссылки Tidal.")
    tid = m.group(1)
    meta = {
        "platform": "tidal",
        "track_id": tid,
        "artist": "Unknown Artist",
        "title": f"Tidal Track {tid}",
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
            f"https://tidal.com/v1/tracks/{tid}",
            params={"countryCode": "US"},
            headers={"x-tidal-token": "49YxDN9a2aFV6RTG", **HEADERS},
            timeout=12,
        )
        if r.status_code == 200:
            d = r.json()
            meta["title"] = d.get("title") or meta["title"]
            meta["mix"] = d.get("version") or None
            artists = [a.get("name") for a in (d.get("artists") or []) if a.get("name")]
            if artists:
                meta["artist"] = ", ".join(artists)
            elif isinstance(d.get("artist"), dict):
                meta["artist"] = d["artist"].get("name") or meta["artist"]
            alb = d.get("album") or {}
            if isinstance(alb, dict):
                meta["album"] = alb.get("title")
                cov = alb.get("cover")
                if cov:
                    meta["cover_url"] = f"https://resources.tidal.com/images/{cov.replace('-', '/')}/1280x1280.jpg"
            meta["duration_sec"] = int(d.get("duration") or 0) or None
            meta["isrc"] = d.get("isrc")
            meta["bpm"] = str(d["bpm"]) if d.get("bpm") else None
            meta["key"] = d.get("key")
            meta["release_date"] = (d.get("streamStartDate") or "")[:10] or None
    except Exception as e:
        logger.warning(f"Tidal metadata warning: {e}")
    return meta


def parse_qobuz_url(url: str) -> dict:
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


def parse_amazon_url(url: str) -> dict:
    parsed = urllib.parse.urlparse(url.strip())
    qs = urllib.parse.parse_qs(parsed.query)
    asin = (qs.get("trackAsin") or qs.get("trackasin") or [""])[0]
    if not asin:
        m = re.search(r"/(?:tracks?|albums?)/([A-Z0-9]{10})", url, re.I)
        if m:
            asin = m.group(1)
    if not asin:
        raise ValueError("Не удалось извлечь ASIN трека из ссылки Amazon Music.")
    asin = asin.upper()
    return {
        "platform": "amazon",
        "track_id": asin,
        "artist": "Amazon Music",
        "title": f"Track {asin}",
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


def parse_deezer_url(url: str) -> dict:
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


# ==================== ПОИСК FULL EXTENDED В TIDAL / QOBUZ / DEEZER / AUDIOMACK ====================
def normalize_for_match(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"[^\w\s]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def find_matching_tidal_track(meta: dict, expected_dur: int = 0) -> str:
    artist = meta.get("artist") or ""
    title = meta.get("title") or ""
    isrc = (meta.get("isrc") or "").strip().upper()
    q = f"{artist} {title}".strip()
    if not q:
        return ""
    try:
        r = requests.get(
            "https://tidal.com/v1/search/tracks",
            params={"query": q, "limit": 15, "countryCode": "US"},
            headers={"x-tidal-token": "49YxDN9a2aFV6RTG", **HEADERS},
            timeout=10,
        )
        if r.status_code != 200:
            return ""
        items = r.json().get("items") or []
        req_art = normalize_for_match(artist.split(",")[0])
        req_tit = normalize_for_match(title)
        for it in items:
            it_isrc = str(it.get("isrc") or "").strip().upper()
            it_dur = int(it.get("duration") or 0)
            if isrc and it_isrc == isrc:
                if not expected_dur or abs(it_dur - expected_dur) <= 20:
                    return str(it["id"])
            it_art = normalize_for_match(" ".join(a.get("name", "") for a in (it.get("artists") or [])))
            it_tit = normalize_for_match(f"{it.get('title', '')} {it.get('version') or ''}")
            if req_art in it_art and req_tit in it_tit:
                if not expected_dur or abs(it_dur - expected_dur) <= 20:
                    return str(it["id"])
    except Exception:
        pass
    return ""


def find_matching_qobuz_track(meta: dict, expected_dur: int = 0) -> str:
    artist = meta.get("artist") or ""
    title = meta.get("title") or ""
    isrc = (meta.get("isrc") or "").strip().upper()
    q = f"{artist} {title}".strip()
    if not q:
        return ""
    try:
        r = requests.get(
            "https://www.qobuz.com/api.json/0.2/track/search",
            params={"query": q, "limit": 15},
            headers={"X-App-Id": "735532640", **HEADERS},
            timeout=10,
        )
        if r.status_code != 200:
            return ""
        items = r.json().get("tracks", {}).get("items") or []
        req_art = normalize_for_match(artist.split(",")[0])
        req_tit = normalize_for_match(title)
        for it in items:
            it_isrc = str(it.get("isrc") or "").strip().upper()
            it_dur = int(it.get("duration") or 0)
            if isrc and it_isrc == isrc:
                if not expected_dur or abs(it_dur - expected_dur) <= 20:
                    return str(it["id"])
            perf = (it.get("performer") or {}).get("name") or ""
            it_art = normalize_for_match(perf)
            it_tit = normalize_for_match(f"{it.get('title', '')} {it.get('version') or ''}")
            if req_art in it_art and req_tit in it_tit:
                if not expected_dur or abs(it_dur - expected_dur) <= 20:
                    return str(it["id"])
    except Exception:
        pass
    return ""


# ==================== ГЛАВНЫЙ ПАЙПЛАЙН СКАЧИВАНИЯ ====================
def process_download_request(url: str, meta: dict, target_format: str, tmp_dir: str, allow_radio_fallback: bool = False):
    """
    Скачивает трек в выбранном формате (flac | wav | mp3):
    1) Если ссылка на Beatport:
       - Сначала качает оригинальный FLAC Lossless (Extended Mix) напрямую с серверов Beatport через ZarzV2 (btp)!
       - Либо через личный аккаунт Beatport API v4 (если настроен).
       - Либо ищет точный Extended Mix по ISRC и длительности в Tidal Hi-Res / Qobuz Hi-Res.
    2) Если ссылка на Tidal -> качает Hi-Res / CD FLAC через ZarzV2 (tid).
    3) Если ссылка на Qobuz -> качает Hi-Res / CD FLAC через ZarzV2 (qbz).
    4) Если ссылка на Amazon Music -> качает Ultra HD FLAC через ZarzV2 (amazeamazeamaze) + расшифровка ffmpeg.
    5) Если ссылка на Deezer -> качает FLAC через ZarzV2 (dzr).
    6) Если прямая ссылка на SoundCloud / Bandcamp -> качает напрямую через yt-dlp.
    """
    platform = (meta.get("platform") or "beatport").lower()
    audio_path = None
    actual_dur = meta.get("duration_sec") or 0

    # 1. Прямая ссылка на SoundCloud или Bandcamp (ТОЛЬКО если пользователь сам её скинул!)
    if platform in ("soundcloud", "bandcamp") or any(d in url.lower() for d in ("soundcloud.com", "bandcamp.com")):
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

    # 2. Прямая ссылка на TIDAL
    elif platform == "tidal":
        audio_path = ZARZ_CLIENT.download_tidal_track(meta["track_id"], target_format, tmp_dir)

    # 3. Прямая ссылка на QOBUZ
    elif platform == "qobuz":
        audio_path = ZARZ_CLIENT.download_qobuz_track(meta["track_id"], target_format, tmp_dir)

    # 4. Прямая ссылка на AMAZON MUSIC
    elif platform == "amazon":
        audio_path, amz_extra = ZARZ_CLIENT.download_amazon_track(meta["track_id"], target_format, tmp_dir)
        for k, v in amz_extra.items():
            if v and (not meta.get(k) or meta.get(k) in ("Amazon Music", f"Track {meta['track_id']}")):
                meta[k] = v

    # 5. Прямая ссылка на DEEZER
    elif platform == "deezer":
        audio_path = ZARZ_CLIENT.download_deezer_track(meta["track_id"], target_format, tmp_dir)

    # 6. Ссылка на BEATPORT (оригинальный Extended Mix в FLAC / WAV / MP3 320!)
    else:
        bp_err = None
        if meta.get("track_id"):
            # Шаг А: Пробуем скачать оригинальный FLAC напрямую с Beatport через бесплатный шлюз ZarzV2 (btp)
            try:
                audio_path = ZARZ_CLIENT.download_beatport_track(meta["track_id"], target_format, tmp_dir)
            except ZarzVerificationRequired:
                raise
            except Exception as e:
                bp_err = e
                logger.warning(f"Zarz Beatport direct warning: {e}")

            # Шаг Б: Если настроен личный токен Beatport API v4
            if not audio_path:
                try:
                    audio_path = download_direct_from_beatport_api(meta["track_id"], target_format, tmp_dir)
                except Exception as e:
                    logger.warning(f"Personal Beatport API v4 warning: {e}")

            # Шаг В: Ищем точный Extended Mix по ISRC и длительности в Tidal / Qobuz через ZarzV2
            if not audio_path:
                expected_dur = 0 if allow_radio_fallback else int(meta.get("duration_sec") or 0)
                tid_match = find_matching_tidal_track(meta, expected_dur=expected_dur)
                if tid_match:
                    try:
                        audio_path = ZARZ_CLIENT.download_tidal_track(tid_match, target_format, tmp_dir)
                    except ZarzVerificationRequired:
                        raise
                    except Exception as e:
                        logger.warning(f"Tidal fallback warning: {e}")

                if not audio_path:
                    qbz_match = find_matching_qobuz_track(meta, expected_dur=expected_dur)
                    if qbz_match:
                        try:
                            audio_path = ZARZ_CLIENT.download_qobuz_track(qbz_match, target_format, tmp_dir)
                        except ZarzVerificationRequired:
                            raise
                        except Exception as e:
                            logger.warning(f"Qobuz fallback warning: {e}")

        if not audio_path:
            err_detail = f" ({bp_err})" if bp_err else ""
            raise ValueError(
                f"Не удалось скачать оригинальный Extended Mix <b>{meta.get('artist')} – {meta.get('title')}</b>{err_detail}."
            )

    # Скачиваем обложку и вшиваем все DJ-теги
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
    zarz_ok = ZARZ_CLIENT.is_valid()
    status_line = (
        "🟢 <b>Lossless-сервер (Beatport / Tidal / Qobuz / Amazon / Deezer):</b> Активен (FLAC / WAV / MP3 320)"
        if zarz_ok
        else "🟡 <b>Lossless-сервер:</b> Нажмите /activate для бесплатной активации в 1 клик (без подписок!)"
    )
    await message.answer(
        "👋 <b>Привет! Я мультиплатформенный Lossless-загрузчик для DJ:</b>\n"
        "• <b>Beatport</b> (полные <b>Extended Mix</b> в <b>FLAC Lossless / WAV / MP3 320</b>)\n"
        "• <b>Tidal</b> (Hi-Res 24-bit / 16-bit FLAC)\n"
        "• <b>Qobuz</b> (Hi-Res 24-bit / CD FLAC)\n"
        "• <b>Amazon Music</b> (Ultra HD / HD FLAC)\n"
        "• <b>Deezer</b> (FLAC Lossless)\n"
        "• <b>SoundCloud</b> (только по прямой ссылке)\n\n"
        f"{status_line}\n\n"
        "💎 <b>Никаких платных подписок покупать не нужно!</b> Просто отправьте ссылку на трек с Beatport, Tidal, Qobuz, Amazon Music или Deezer.",
        parse_mode="HTML",
    )


@dp.message(Command("activate", "zarz", "bp_status"))
async def cmd_activate(message: types.Message):
    if ZARZ_CLIENT.is_valid():
        await message.answer(
            f"🟢 <b>Lossless-шлюз (Beatport / Tidal / Qobuz / Amazon / Deezer) уже активен!</b>\n"
            f"Сессия действует до: <code>{ZARZ_CLIENT.expires_at}</code> (продлевается автоматически).\n\n"
            f"Можете сразу отправлять ссылки на Beatport, Tidal, Qobuz, Amazon Music или Deezer!",
            parse_mode="HTML",
        )
        return
    try:
        auth_url = await asyncio.to_thread(ZARZ_CLIENT.bootstrap_challenge_url)
        if not auth_url:
            await message.answer("🟢 <b>Lossless-шлюз успешно активирован автоматически!</b>", parse_mode="HTML")
            return
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔐 Открыть страницу активации (1 клик)", url=auth_url)]
            ]
        )
        await message.answer(
            "🔓 <b>Бесплатная активация Lossless-сервера (Beatport / Tidal / Qobuz / Amazon FLAC):</b>\n\n"
            "1️⃣ Нажмите кнопку ниже и поставьте галочку Cloudflare.\n"
            "2️⃣ Нажмите зелёную кнопку <b>«Copy link»</b> на странице.\n"
            "3️⃣ Просто <b>вставьте скопированную ссылку</b> (начинается на <code>spotiflac://...</code>) в этот чат!\n\n"
            "<i>После этого бот будет бесплатно качать оригинальные FLAC / WAV / MP3 320 с Beatport, Tidal, Qobuz и Amazon Music и сам автоматически продлевать сессию!</i>",
            reply_markup=kb,
            parse_mode="HTML",
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка получения ссылки активации: {e}")


@dp.message(Command("bp_login"))
async def cmd_bp_login(message: types.Message):
    parts = message.text.strip().split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("Использование: <code>/bp_login ваш_email ваш_пароль</code>", parse_mode="HTML")
        return
    _, email, password = parts
    with open(BP_CREDS_FILE, "w", encoding="utf-8") as f:
        json.dump({"username": email, "password": password}, f)
    _BP_TOKEN_CACHE["access_token"] = ""
    try:
        tok = await asyncio.to_thread(bp_login_and_get_token)
        if tok:
            await message.answer("✅ <b>Аккаунт Beatport успешно привязан!</b>", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ <b>Не удалось войти в Beatport:</b>\n<code>{e}</code>", parse_mode="HTML")


async def execute_download_and_send(chat_id: int, status_msg: types.Message, req_id: str, fmt: str, allow_radio: bool = False):
    task_data = _PENDING_TRACKS.get(req_id)
    if not task_data:
        await status_msg.edit_text("⚠️ Сессия трека устарела, отправьте ссылку заново.")
        return

    url = task_data["url"]
    meta = task_data["meta"]
    clean_artist = meta.get("artist") or "Artist"
    clean_title = meta.get("title") or "Track"
    if meta.get("mix") and meta["mix"].lower() not in clean_title.lower():
        clean_title = f"{clean_title} ({meta['mix']})"

    platform_names = {
        "beatport": "Beatport Lossless",
        "tidal": "Tidal Hi-Res / FLAC",
        "qobuz": "Qobuz Hi-Res / FLAC",
        "amazon": "Amazon Music Ultra HD",
        "deezer": "Deezer FLAC",
        "soundcloud": "SoundCloud",
    }
    plat_label = platform_names.get(meta.get("platform", "beatport"), "Lossless")

    await status_msg.edit_text(
        f"⏳ <b>{clean_artist} – {clean_title}</b>\n"
        f"Источник: <b>{plat_label}</b> | Формат: <b>{fmt.upper()}</b>\n"
        f"Скачиваем оригинал и вшиваем теги...",
        parse_mode="HTML",
    )

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            file_path, cover_path, actual_dur = await asyncio.wait_for(
                asyncio.to_thread(process_download_request, url, meta, fmt, tmp_dir, allow_radio),
                timeout=95.0,
            )
            # Перечитываем обновлённые метаданные (например, с Amazon Music)
            clean_artist = meta.get("artist") or clean_artist
            clean_title = meta.get("title") or clean_title
            if meta.get("mix") and meta["mix"].lower() not in clean_title.lower():
                clean_title = f"{clean_title} ({meta['mix']})"

            caption_parts = [f"🎵 <b>{clean_artist} – {clean_title}</b>"]
            caption_parts.append(f"💎 <b>Source:</b> {plat_label} ({fmt.upper()})")
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
                caption_parts.append(f"⏱ <b>Length:</b> {format_mm_ss(actual_dur)}")
            caption_parts.append(
                "\n🎧 <a href='https://musicvibez.org'>MusicVibez.org</a> • <a href='https://specialfordjs.org'>SpecialForDJs.org</a>"
            )

            ext = fmt.lower()
            clean_filename = f"{clean_artist} - {clean_title}.{ext}".replace("/", "_").replace("\\", "_").replace(":", "")
            audio_file = FSInputFile(file_path, filename=clean_filename)
            thumb_file = FSInputFile(cover_path) if cover_path and os.path.exists(cover_path) else None

            await status_msg.edit_text(f"🚀 Выгружаю <b>{fmt.upper()}</b> в Telegram...", parse_mode="HTML")
            await bot.send_audio(
                chat_id=chat_id,
                audio=audio_file,
                title=clean_title,
                performer=clean_artist,
                duration=actual_dur if actual_dur and actual_dur > 0 else None,
                thumbnail=thumb_file,
                caption="\n".join(caption_parts),
                parse_mode="HTML",
            )
            await status_msg.delete()
    except ZarzVerificationRequired as zv:
        _WAITING_FOR_GRANT[chat_id] = {"req_id": req_id, "fmt": fmt, "allow_radio": allow_radio}
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔐 Активировать сервер в 1 клик (бесплатно)", url=zv.auth_url)]
            ]
        )
        await status_msg.edit_text(
            f"🔓 <b>Требуется разовая активация бесплатного Lossless-сервера!</b>\n\n"
            f"Чтобы скачать <b>{clean_artist} – {clean_title}</b> в <b>{fmt.upper()}</b> напрямую без платных подписок:\n"
            f"1️⃣ Нажмите кнопку ниже и пройдите проверку Cloudflare (1 сек).\n"
            f"2️⃣ Нажмите зелёную кнопку <b>«Copy link»</b>.\n"
            f"3️⃣ Отправьте скопированную ссылку (<code>spotiflac://...</code>) прямо сюда в чат — и я <b>сразу же пришлю этот трек!</b>",
            reply_markup=kb,
            parse_mode="HTML",
        )
    except Exception as e:
        logger.exception("Download execution error")
        await status_msg.edit_text(f"❌ {str(e)[:400]}", parse_mode="HTML")


@dp.message(F.text)
async def handle_link(message: types.Message):
    text = message.text.strip()

    # 1. Проверяем, не прислал ли пользователь ссылку активации spotiflac:// или grant=...
    if "spotiflac://" in text or "mornye://" in text or "grant=" in text or text.startswith("grt_"):
        status_msg = await message.answer("⏳ Проверяю код активации Lossless-сервера...")
        try:
            await asyncio.to_thread(ZARZ_CLIENT.exchange_grant, text)
            waiting = _WAITING_FOR_GRANT.pop(message.chat.id, None)
            if waiting and waiting["req_id"] in _PENDING_TRACKS:
                await status_msg.edit_text(
                    "✅ <b>Lossless-сервер успешно активирован!</b>\nНачинаю скачивание вашего трека...",
                    parse_mode="HTML",
                )
                await execute_download_and_send(
                    message.chat.id,
                    status_msg,
                    waiting["req_id"],
                    waiting["fmt"],
                    waiting["allow_radio"],
                )
            else:
                await status_msg.edit_text(
                    "✅ <b>Lossless-сервер (Beatport / Tidal / Qobuz / Amazon / Deezer) успешно активирован!</b>\n"
                    f"Сессия активна до: <code>{ZARZ_CLIENT.expires_at}</code> и будет продлеваться автоматически.\n\n"
                    "Отправьте ссылку на трек!",
                    parse_mode="HTML",
                )
        except Exception as e:
            await status_msg.edit_text(f"❌ Не удалось активировать сессию: {e}\nПопробуйте нажать /activate заново.")
        return

    url_match = re.search(r"(https?://\S+)", text)
    if not url_match:
        await message.answer(
            "Отправьте ссылку на трек с <b>Beatport</b>, <b>Tidal</b>, <b>Qobuz</b>, <b>Amazon Music</b>, <b>Deezer</b> или <b>SoundCloud</b>.",
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
                    InlineKeyboardButton(text="💎 FLAC (Lossless)", callback_data=f"dl|{req_id}|flac|0"),
                    InlineKeyboardButton(text="💿 WAV", callback_data=f"dl|{req_id}|wav|0"),
                    InlineKeyboardButton(text="🎧 MP3 (320 kbps)", callback_data=f"dl|{req_id}|mp3|0"),
                ]
            ]
        )
        await message.answer(
            "☁️ <b>Прямая ссылка SoundCloud / Bandcamp</b>\nВыберите формат для скачивания:",
            reply_markup=kb,
            parse_mode="HTML",
        )
        return

    # Определяем платформу (Beatport / Tidal / Qobuz / Amazon Music / Deezer)
    status_msg = await message.answer("🔍 Читаю метаданные трека...")
    try:
        if "beatport.com" in low_url:
            meta = await asyncio.wait_for(asyncio.to_thread(parse_beatport, url), timeout=15.0)
            plat_badge = "🎧 <b>Beatport Direct Lossless</b>"
        elif "tidal.com" in low_url:
            meta = await asyncio.wait_for(asyncio.to_thread(parse_tidal_url, url), timeout=15.0)
            plat_badge = "🌊 <b>Tidal Hi-Res / Lossless</b>"
        elif "qobuz.com" in low_url:
            meta = await asyncio.wait_for(asyncio.to_thread(parse_qobuz_url, url), timeout=15.0)
            plat_badge = "💿 <b>Qobuz Hi-Res / Lossless</b>"
        elif "amazon." in low_url:
            meta = await asyncio.wait_for(asyncio.to_thread(parse_amazon_url, url), timeout=15.0)
            plat_badge = "📦 <b>Amazon Music Ultra HD / FLAC</b>"
        elif "deezer." in low_url:
            meta = await asyncio.wait_for(asyncio.to_thread(parse_deezer_url, url), timeout=15.0)
            plat_badge = "💜 <b>Deezer Lossless FLAC</b>"
        else:
            await status_msg.edit_text(
                "⚠️ Поддерживаются ссылки на:\n"
                "• <b>Beatport</b>\n• <b>Tidal</b>\n• <b>Qobuz</b>\n• <b>Amazon Music</b>\n• <b>Deezer</b>\n• <b>SoundCloud</b>",
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
    lines.append("\n👇 <b>Выберите формат (рекомендуется FLAC Lossless):</b>")

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="💎 FLAC (Lossless)", callback_data=f"dl|{req_id}|flac|0"),
                InlineKeyboardButton(text="💿 WAV", callback_data=f"dl|{req_id}|wav|0"),
                InlineKeyboardButton(text="🎧 MP3 (320 kbps)", callback_data=f"dl|{req_id}|mp3|0"),
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

    if req_id not in _PENDING_TRACKS:
        await call.answer("Сессия трека устарела, отправьте ссылку заново.", show_alert=True)
        return

    await call.answer(f"Готовлю {fmt.upper()}...")
    await execute_download_and_send(call.message.chat.id, call.message, req_id, fmt, allow_radio)


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
