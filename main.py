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

# ------------------- Env variables (no dotenv) -------------------
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
BOT_OWNER_ID = int(os.environ.get("BOT_OWNER_ID", "0"))

# ------------------- Config -------------------
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/data/downloads")
TELEGRAM_MAX_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB
PROGRESS_POLL_INTERVAL = 2
CLEANUP_AGE_HOURS = 6

# ------------------- Setup -------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("mega_bot")
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

jobs = {}
jobs_lock = threading.Lock()

# ------------------- Utilities -------------------

def is_mega_link(url: str):
    return bool(re.match(r"https://mega\.nz/(file|folder)/[A-Za-z0-9_-]+#[A-Za-z0-9_-]+", url))

def human_size(n):
    try:
        return humanize.naturalsize(n, binary=True)
    except Exception:
        return f"{round(n/1024/1024,2)} MB"

def safe_edit_message(msg, text):
    try:
        bot.edit_message_text(text, msg.chat.id, msg.message_id, parse_mode="Markdown")
    except Exception as e:
        if "message not modified" not in str(e).lower():
            logger.debug(f"edit_message error: {e}")

def cleanup_dir(path):
    try:
        if os.path.exists(path):
            shutil.rmtree(path)
            logger.info(f"Cleaned up: {path}")
    except Exception as e:
        logger.warning(f"Cleanup failed for {path}: {e}")

# ------------------- Progress monitor -------------------

def get_total_size_of_path(path):
    total = 0
    if not os.path.exists(path):
        return 0
    if os.path.isfile(path):
        return os.path.getsize(path)
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except Exception:
                pass
    return total

def progress_text_bar(downloaded_bytes):
    human = human_size(downloaded_bytes)
    return f"📥 Downloaded so far: `{human}`"

def monitor_download_progress(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return
        status_msg = job['status_msg']
        download_dir = job['download_dir']

    try:
        while True:
            with jobs_lock:
                job = jobs.get(job_id)
                if not job:
                    return
                if job.get('cancel_requested'):
                    safe_edit_message(status_msg, f"❌ Download cancelled by user.")
                    return
                done = job.get('done', False)

            size_now = get_total_size_of_path(download_dir)
            text = (
                f"🔗 Processing Mega link:\n`{job['url']}`\n\n"
                f"⏳ Started: `{job['started_at'].strftime('%Y-%m-%d %H:%M:%S')}`\n"
                f"{progress_text_bar(size_now)}\n\n"
                f"_Updating every {PROGRESS_POLL_INTERVAL}s..._"
            )
            safe_edit_message(status_msg, text)
            if done:
                return
            time.sleep(PROGRESS_POLL_INTERVAL)
    except Exception as e:
        logger.exception(f"monitor_download_progress error for {job_id}: {e}")

# ------------------- Downloader thread -------------------

def download_and_handle(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return
        url = job['url']
        chat_id = job['chat_id']
        status_msg = job['status_msg']
        download_dir = job['download_dir']
        user_id = job['user_id']

    m = Megatools()
    try:
        safe_edit_message(status_msg, "⬇️ Download started...")
        m.download(url, path=download_dir)

        files = []
        for root, _, filenames in os.walk(download_dir):
            for fn in filenames:
                files.append(os.path.join(root, fn))

        with jobs_lock:
            job['files'] = files
            job['done'] = True

        if job.get('cancel_requested'):
            safe_edit_message(status_msg, "❌ Download cancelled. Cleaning up...")
            cleanup_dir(download_dir)
            return

        if not files:
            safe_edit_message(status_msg, "⚠️ Download finished but no files found.")
            cleanup_dir(download_dir)
            return

        info_lines = []
        total_bytes = 0
        for p in files:
            s = os.path.getsize(p)
            total_bytes += s
            info_lines.append(f"• `{os.path.basename(p)}` — {human_size(s)}")

        header = (
            f"✅ Download complete!\n\n"
            f"📄 Files ({len(files)}):\n" + "\n".join(info_lines) + "\n\n"
            f"Total: `{human_size(total_bytes)}`"
        )
        safe_edit_message(status_msg, header)

        if total_bytes > TELEGRAM_MAX_BYTES:
            safe_edit_message(status_msg, header + f"\n\n⚠️ File too large for Telegram upload (>2GB). Stored at `{download_dir}`.")
            return

        for idx, p in enumerate(files, start=1):
            if job.get('cancel_requested'):
                safe_edit_message(status_msg, "❌ Upload cancelled by user. Cleaning up...")
                cleanup_dir(download_dir)
                return
            fname = os.path.basename(p)
            try:
                safe_edit_message(status_msg, f"📤 Uploading {idx}/{len(files)}: `{fname}`")
                upload_file_with_type(chat_id, p, fname)
            except Exception as e:
                logger.exception(f"Upload failed for {p}: {e}")
                bot.send_message(chat_id, f"⚠️ Failed to upload `{fname}`: {e}")

        safe_edit_message(status_msg, f"✅ Upload finished successfully.")
    except MegaError as me:
        logger.exception("Megatools error", exc_info=True)
        safe_edit_message(status_msg, f"❌ Mega.nz Error: `{me}`")
    except Exception as e:
        logger.exception("Download thread error", exc_info=True)
        safe_edit_message(status_msg, f"❌ Unexpected error: `{e}`")
    finally:
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id]['done'] = True
        cleanup_dir(download_dir)

# ------------------- Upload helper -------------------

def upload_file_with_type(chat_id, file_path, filename):
    ext = os.path.splitext(filename)[1].lower()
    caption = f"📄 `{filename}`\nSize: {human_size(os.path.getsize(file_path))}"
    with open(file_path, "rb") as f:
        if ext in [".mp4", ".mkv", ".mov", ".avi", ".webm"]:
            try:
                bot.send_video(chat_id, f, caption=caption, supports_streaming=True, has_spoiler=True)
            except TypeError:
                f.seek(0)
                bot.send_video(chat_id, f, caption=caption, supports_streaming=True)
        elif ext in [".mp3", ".wav", ".m4a", ".ogg", ".flac"]:
            try:
                bot.send_audio(chat_id, f, caption=caption)
            except Exception:
                f.seek(0)
                bot.send_document(chat_id, f, caption=caption)
        elif ext in [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"]:
            try:
                bot.send_photo(chat_id, f, caption=caption)
            except Exception:
                f.seek(0)
                bot.send_document(chat_id, f, caption=caption)
        else:
            try:
                bot.send_document(chat_id, f, caption=caption, has_spoiler=False)
            except TypeError:
                f.seek(0)
                bot.send_document(chat_id, f, caption=caption)

# ------------------- Commands -------------------

@bot.message_handler(commands=["start"])
def start(message):
    bot.reply_to(message,
        "👋 *Mega.nz Advanced Bot*\nSend any Mega.nz `file` or `folder` link.\n"
        "Owner commands: `/status`, `/cancel <job_id>`, `/clear`",
        parse_mode="Markdown")

@bot.message_handler(commands=["status"])
def status(message):
    if message.from_user.id != BOT_OWNER_ID:
        bot.reply_to(message, "❌ Owner only.")
        return
    with jobs_lock:
        if not jobs:
            bot.reply_to(message, "ℹ️ No active jobs.")
            return
        lines = []
        for jid, j in jobs.items():
            state = "✅ done" if j.get('done') else "⏳ running"
            lines.append(f"`{jid}` — {state} — {j.get('url')}")
    bot.reply_to(message, "\n".join(lines))

@bot.message_handler(commands=["cancel"])
def cancel(message):
    if message.from_user.id != BOT_OWNER_ID:
        bot.reply_to(message, "❌ Owner only.")
        return
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "Usage: /cancel <job_id>")
        return
    jid = args[1]
    with jobs_lock:
        if jid in jobs:
            jobs[jid]['cancel_requested'] = True
            bot.reply_to(message, f"⚠️ Cancel requested for `{jid}`")
        else:
            bot.reply_to(message, f"❌ Job `{jid}` not found")

@bot.message_handler(commands=["clear"])
def clear(message):
    if message.from_user.id != BOT_OWNER_ID:
        bot.reply_to(message, "❌ Owner only.")
        return
    cutoff = datetime.utcnow() - timedelta(hours=CLEANUP_AGE_HOURS)
    removed = []
    for entry in os.listdir(DOWNLOAD_DIR):
        p = os.path.join(DOWNLOAD_DIR, entry)
        try:
            mtime = datetime.utcfromtimestamp(os.path.getmtime(p))
            if mtime < cutoff:
                cleanup_dir(p)
                removed.append(entry)
        except Exception:
            pass
    bot.reply_to(message, f"🧹 Cleared {len(removed)} old folders.")

# ------------------- Mega Link Handler -------------------

@bot.message_handler(func=lambda m: isinstance(m.text, str) and is_mega_link(m.text.strip()))
def handle_mega(message):
    url = message.text.strip()
    chat_id = message.chat.id
    user_id = message.from_user.id

    job_id = str(uuid.uuid4())[:8]
    download_dir = os.path.join(DOWNLOAD_DIR, f"user_{user_id}_{job_id}")
    os.makedirs(download_dir, exist_ok=True)
    status_msg = bot.send_message(chat_id, f"🔗 Job `{job_id}` started\nProcessing `{url}`")

    job = {
        'user_id': user_id,
        'chat_id': chat_id,
        'url': url,
        'status_msg': status_msg,
        'download_dir': download_dir,
        'files': [],
        'started_at': datetime.utcnow(),
        'cancel_requested': False,
        'done': False,
    }

    with jobs_lock:
        jobs[job_id] = job

    threading.Thread(target=monitor_download_progress, args=(job_id,), daemon=True).start()
    threading.Thread(target=download_and_handle, args=(job_id,), daemon=True).start()

@bot.message_handler(func=lambda m: True)
def other(message):
    bot.reply_to(message, "⚡ Send a valid Mega.nz link to start downloading.")

# ------------------- Run Bot -------------------
if __name__ == "__main__":
    logger.info("🚀 Advanced Mega.nz bot running (no dotenv)...")
    bot.infinity_polling()
