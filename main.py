#!/usr/bin/env python3
import os
import re
import time
import uuid
import shutil
import threading
import logging
import humanize
from datetime import datetime, timedelta
import telebot
from telebot import types
from pymegatools import Megatools
from pymegatools.pymegatools import MegaError

# ------------------- Env variables -------------------
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
BOT_OWNER_ID = int(os.environ.get("BOT_OWNER_ID", "0"))

# ------------------- Config -------------------
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/data/downloads")
TELEGRAM_MAX_BYTES = 2 * 1024 * 1024 * 1024  # 2GB
PROGRESS_POLL_INTERVAL = 3
CLEANUP_AGE_HOURS = 6

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("mega_live_bot")
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
jobs = {}
jobs_lock = threading.Lock()

# ------------------- Helpers -------------------
def is_mega_link(url: str):
    return bool(re.match(r"https://mega\.nz/(file|folder)/[A-Za-z0-9_-]+#[A-Za-z0-9_-]+", url))

def human_size(n):
    return humanize.naturalsize(n, binary=True)

def safe_edit_message(msg, text):
    try:
        bot.edit_message_text(text, msg.chat.id, msg.message_id, parse_mode="Markdown")
    except Exception:
        pass

def cleanup_dir(path):
    try:
        if os.path.exists(path):
            shutil.rmtree(path)
    except Exception:
        pass

def get_total_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except Exception:
                pass
    return total

def upload_file_auto(chat_id, file_path):
    fname = os.path.basename(file_path)
    caption = f"📄 `{fname}`\nSize: {human_size(os.path.getsize(file_path))}"
    with open(file_path, "rb") as f:
        ext = os.path.splitext(fname)[1].lower()
        try:
            if ext in [".mp4", ".mkv", ".mov", ".avi", ".webm"]:
                bot.send_video(chat_id, f, caption=caption, supports_streaming=True, has_spoiler=True)
            elif ext in [".mp3", ".wav", ".m4a", ".ogg"]:
                bot.send_audio(chat_id, f, caption=caption)
            elif ext in [".jpg", ".jpeg", ".png", ".gif", ".webp"]:
                bot.send_photo(chat_id, f, caption=caption)
            else:
                bot.send_document(chat_id, f, caption=caption)
        except Exception as e:
            logger.warning(f"Upload error {fname}: {e}")

# ------------------- Real-time Download & Upload -------------------
def live_download(job_id):
    with jobs_lock:
        job = jobs[job_id]
        url = job['url']
        chat_id = job['chat_id']
        status_msg = job['status_msg']
        download_dir = job['download_dir']

    safe_edit_message(status_msg, f"🚀 Starting live download:\n`{url}`")

    m = Megatools()
    try:
        before_files = set()
        threading.Thread(target=progress_monitor, args=(job_id,), daemon=True).start()

        for path in m.iter_download(url, path=download_dir):  # iter_download yields as it downloads
            if not path:
                continue

            current_files = set()
            for root, _, files in os.walk(download_dir):
                for f in files:
                    current_files.add(os.path.join(root, f))

            new_files = current_files - before_files
            before_files = current_files

            for nf in new_files:
                size = os.path.getsize(nf)
                if size > TELEGRAM_MAX_BYTES:
                    bot.send_message(chat_id, f"⚠️ `{os.path.basename(nf)}` too large to upload (>2GB)")
                    continue
                upload_file_auto(chat_id, nf)

        safe_edit_message(status_msg, "✅ All files processed & uploaded.")
    except MegaError as e:
        safe_edit_message(status_msg, f"❌ Mega.nz Error: `{e}`")
    except Exception as e:
        safe_edit_message(status_msg, f"❌ Error: `{e}`")
    finally:
        cleanup_dir(download_dir)
        with jobs_lock:
            job['done'] = True

def progress_monitor(job_id):
    with jobs_lock:
        job = jobs[job_id]
        status_msg = job['status_msg']
        download_dir = job['download_dir']

    while True:
        with jobs_lock:
            done = job.get("done")
        if done:
            break
        total_size = get_total_size(download_dir)
        text = (
            f"📥 *Downloading...*\n"
            f"Progress: `{human_size(total_size)}` so far\n"
            f"Updating every {PROGRESS_POLL_INTERVAL}s..."
        )
        safe_edit_message(status_msg, text)
        time.sleep(PROGRESS_POLL_INTERVAL)

# ------------------- Bot Commands -------------------
@bot.message_handler(commands=["start"])
def start(message):
    bot.reply_to(message, "👋 *Mega.nz Live Downloader*\nSend any Mega.nz link and files will auto-upload as soon as downloaded.")

@bot.message_handler(func=lambda m: isinstance(m.text, str) and is_mega_link(m.text.strip()))
def handle_mega(message):
    url = message.text.strip()
    job_id = str(uuid.uuid4())[:8]
    user_id = message.from_user.id
    download_dir = os.path.join(DOWNLOAD_DIR, f"user_{user_id}_{job_id}")
    os.makedirs(download_dir, exist_ok=True)
    status_msg = bot.send_message(message.chat.id, f"🔗 Job `{job_id}` started.\nDownloading `{url}`")

    job = {
        'user_id': user_id,
        'chat_id': message.chat.id,
        'url': url,
        'status_msg': status_msg,
        'download_dir': download_dir,
        'started_at': datetime.utcnow(),
        'done': False
    }

    with jobs_lock:
        jobs[job_id] = job

    threading.Thread(target=live_download, args=(job_id,), daemon=True).start()

@bot.message_handler(func=lambda m: True)
def fallback(message):
    bot.reply_to(message, "⚡ Send a valid Mega.nz file/folder link to start downloading.")

# ------------------- Run Bot -------------------
if __name__ == "__main__":
    logger.info("🚀 Mega.nz Live Downloader running...")
    bot.infinity_polling()
