cat << 'EOF' > main.py
import os
import asyncio
import logging
from typing import Any, Dict, Optional
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.methods import TelegramMethod
import yt_dlp
from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3, APIC

BOT_TOKEN = "8881412253:AAELisPKS06kE8kIUG2kXZLfo-Jc8wHBMjk"
ADMIN_ID = 96349161
PROXY_URL = "http://proxy.server:3128"

logging.basicConfig(level=logging.INFO)

class CustomAiohttpSession(AiohttpSession):
    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: Optional[int] = None,
    ) -> Any:
        return await super().make_request(bot, method, timeout=timeout)

session = CustomAiohttpSession(proxy=PROXY_URL)
bot = Bot(token=BOT_TOKEN, session=session)
dp = Dispatcher()

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

def download_audio(url: str) -> dict:
    out_template = os.path.join(DOWNLOAD_DIR, '%(id)s.%(ext)s')
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': out_template,
        'writethumbnail': True,
        'proxy': PROXY_URL,
        'quiet': True,
        'nocheckcertificate': True
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        
        cover_file = None
        base_path = os.path.splitext(filename)[0]
        for ext in ['.jpg', '.webp', '.png', '.jpeg']:
            if os.path.exists(f"{base_path}{ext}"):
                cover_file = f"{base_path}{ext}"
                break
                
        return {
            'file_path': filename,
            'cover_path': cover_file,
            'title': info.get('title', 'Unknown Title'),
            'artist': info.get('artist') or info.get('uploader', 'Unknown Artist'),
            'album': info.get('album', '')
        }

def apply_metadata(file_path: str, title: str, artist: str, album: str, cover_path: str = None):
    if not file_path.endswith('.mp3'):
        return
        
    try:
        audio = EasyID3(file_path)
    except Exception:
        audio = EasyID3()
        audio.save(file_path)
    audio['title'] = title
    audio['artist'] = artist
    if album:
        audio['album'] = album
    audio.save(file_path)

    if cover_path and os.path.exists(cover_path):
        try:
            audio_id3 = ID3(file_path)
            with open(cover_path, 'rb') as albumart:
                audio_id3.add(APIC(
                    encoding=3,
                    mime='image/jpeg' if cover_path.endswith(('.jpg', '.jpeg')) else 'image/png',
                    type=3,
                    desc='Cover',
                    data=albumart.read()
                ))
            audio_id3.save(file_path)
        except Exception:
            pass

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("Привет! Отправь мне ссылку на трек или релиз для скачивания.")

@dp.message(F.text)
async def handle_download(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    url = message.text.strip()
    status_msg = await message.answer("⏳ Скачиваю и обрабатываю трек...")
    try:
        data = await asyncio.to_thread(download_audio, url)
        apply_metadata(data['file_path'], data['title'], data['artist'], data['album'], data['cover_path'])
        
        await bot.send_audio(
            chat_id=message.chat.id,
            audio=types.FSInputFile(data['file_path']),
            title=data['title'],
            performer=data['artist']
        )
        
        if os.path.exists(data['file_path']):
            os.remove(data['file_path'])
        if data['cover_path'] and os.path.exists(data['cover_path']):
            os.remove(data['cover_path'])
            
        await status_msg.delete()
    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка: {e}")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
EOF
