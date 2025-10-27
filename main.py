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
from dotenv import load_dotenv

# ------------------- Load env (make sure .env is private) -------------------
load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID", "0"))

# ------------------- Config -------------------
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "/data/downloads")
TELEGRAM_MAX_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB limit safety
PROGRESS_POLL_INTERVAL = 2  # seconds between progress message updates
CLEANUP_AGE_HOURS = 6  # older downloaded job folders to cleanup with /clear

# ------------------- Setup -------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("mega_bot")
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")

# Ensure download dir exists
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ------------------- Global job store -------------------
# jobs[job_id] = {
#   'user_id': int,
#   'chat_id': int,
#   'url': str,
#   'status_msg': message_object,
#   'download_dir': str,
#   'files': [paths],
#   'started_at': datetime,
#   'cancel_requested': False,
#   'done': False,
# }
jobs = {}
jobs_lock = threading.Lock()

# ------------------- Utilities -------------------

def is_mega_link(url: str):
    pattern = r"https://mega\.nz/(file|folder)/[A-Za-z0-9_-]+#[A-Za-z0-9_-]+"
    return bool(re.match(pattern, url))


def human_size(n):
    try:
        return humanize.naturalsize(n, binary=True)
    except Exception:
        return f"{round(n/1024/1024,2)} MB"


def safe_edit_message(msg, text):
    try:
        bot.edit_message_text(text, msg.chat.id, msg.message_id, parse_mode="Markdown")
    except Exception as e:
        # ignore "message not modified" and other transient errors
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
    # simple textual progress (we don't know total bytes for Mega downloads)
    human = human_size(downloaded_bytes)
    return f"📥 Downloaded so far: `{human}`"


def monitor_download_progress(job_id):
    """
    Periodically checks the download directory size and updates the status message.
    Stops when job marked done or cancelled.
    """
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
                # final update will be performed by the download thread
                return

            time.sleep(PROGRESS_POLL_INTERVAL)
    except Exception as e:
        logger.exception(f"monitor_download_progress error for {job_id}: {e}")


# ------------------- Downloader thread -------------------

def download_and_handle(job_id):
    """
    Thread worker: downloads with Megatools, waits, then uploads or handles large-file policy.
    """
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
        # Try to download. For folder links, Megatools will create a directory inside download_dir.
        safe_edit_message(status_msg, "⬇️ Download started...")

        # This is blocking; progress monitor thread will report interim sizes.
        # We call download with path=download_dir so it stores files inside that folder.
        m.download(url, path=download_dir)

        # After download completes, list files
        files = []
        for root, _, filenames in os.walk(download_dir):
            for fn in filenames:
                files.append(os.path.join(root, fn))

        with jobs_lock:
            job['files'] = files
            job['done'] = True

        # If cancel requested during download, cleanup and exit
        with jobs_lock:
            if job.get('cancel_requested'):
                safe_edit_message(status_msg, "❌ Download cancelled. Cleaning up...")
                cleanup_dir(download_dir)
                return

        if not files:
            safe_edit_message(status_msg, "⚠️ Download finished but no files found.")
            cleanup_dir(download_dir)
            return

        # send info about files
        info_lines = []
        total_bytes = 0
        for p in files:
            try:
                s = os.path.getsize(p)
            except Exception:
                s = 0
            total_bytes += s
            info_lines.append(f"• `{os.path.basename(p)}` — {human_size(s)}")

        header = (
            f"✅ Download complete!\n\n"
            f"📄 Files ({len(files)}):\n" + "\n".join(info_lines) + "\n\n"
            f"Total: `{human_size(total_bytes)}`"
        )
        safe_edit_message(status_msg, header)

        # Decide upload or not based on size
        if total_bytes > TELEGRAM_MAX_BYTES:
            safe_edit_message(status_msg,
                              header + f"\n\n⚠️ Total size exceeds Telegram limit ({human_size(TELEGRAM_MAX_BYTES)}). Upload skipped.\nLocal path: `{download_dir}`\nOwner can `/clear` this later.")
            # leave files for manual handling / cleanup
            return

        # Upload files one by one
        uploaded_count = 0
        for idx, p in enumerate(files, start=1):
            with jobs_lock:
                if job.get('cancel_requested'):
                    safe_edit_message(status_msg, "❌ Upload cancelled by user. Cleaning up...")
                    cleanup_dir(download_dir)
                    return

            fname = os.path.basename(p)
            try:
                safe_edit_message(status_msg, f"📤 Uploading {idx}/{len(files)}: `{fname}`")
                upload_file_with_type(chat_id, p, fname)
                uploaded_count += 1
            except Exception as e:
                logger.exception(f"Upload failed for {p}: {e}")
                bot.send_message(chat_id, f"⚠️ Failed to upload `{fname}`: {e}")

        safe_edit_message(status_msg, f"✅ Upload finished. Sent {uploaded_count}/{len(files)} file(s).")
    except MegaError as me:
        logger.exception("Megatools error", exc_info=True)
        safe_edit_message(status_msg, f"❌ Mega.nz Error: `{me}`")
    except Exception as e:
        logger.exception("Download thread error", exc_info=True)
        safe_edit_message(status_msg, f"❌ Unexpected error: `{e}`")
    finally:
        # Mark job done and possibly cleanup
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id]['done'] = True
        # Optionally cleanup small files after uploading
        # We'll remove download dir only if upload succeeded or exceed limit
        # If user wants files retained, they can /status and request /clear
        # For now we remove the dir if files were uploaded
        try:
            # if files were uploaded (or total size <= limit) then cleanup
            if os.path.exists(download_dir):
                cleanup_dir(download_dir)
        except Exception as e:
            logger.debug(f"Final cleanup error: {e}")


# ------------------- Upload helper (spoiler-safe) -------------------

def upload_file_with_type(chat_id, file_path, filename):
    ext = os.path.splitext(filename)[1].lower()
    caption = f"📄 `{filename}`\nSize: {human_size(os.path.getsize(file_path))}"
    with open(file_path, "rb") as f:
        # Video
        if ext in [".mp4", ".mkv", ".mov", ".avi", ".webm"]:
            # Try has_spoiler for videos, fallback if not supported
            try:
                bot.send_video(chat_id, f, caption=caption, supports_streaming=True, has_spoiler=True)
            except TypeError:
                # library doesn't support has_spoiler
                f.seek(0)
                bot.send_video(chat_id, f, caption=caption, supports_streaming=True)
        # Audio
        elif ext in [".mp3", ".wav", ".m4a", ".ogg", ".flac"]:
            try:
                bot.send_audio(chat_id, f, caption=caption)
            except Exception:
                f.seek(0)
                bot.send_document(chat_id, f, caption=caption)
        # Image
        elif ext in [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"]:
            try:
                bot.send_photo(chat_id, f, caption=caption)
            except Exception:
                f.seek(0)
                bot.send_document(chat_id, f, caption=caption)
        # Documents / archives / others
        else:
            try:
                # Do not pass has_spoiler to send_document unless supported
                try:
                    bot.send_document(chat_id, f, caption=caption, has_spoiler=False)
                except TypeError:
                    f.seek(0)
                    bot.send_document(chat_id, f, caption=caption)
            except Exception:
                # Fallback: send as document without extra args
                f.seek(0)
                bot.send_document(chat_id, f, caption=caption)


# ------------------- Bot handlers (commands) -------------------

@bot.message_handler(commands=["start"])
def cmd_start(message):
    bot.reply_to(message,
                 "👋 *Mega.nz Advanced Bot*\nSend a public `https://mega.nz/file/...` or `https://mega.nz/folder/...` link and I'll fetch & upload it.\nOwner commands: `/status`, `/cancel <job_id>`, `/clear`",
                 parse_mode="Markdown")


@bot.message_handler(commands=["status"])
def cmd_status(message):
    # only owner can use detailed status
    user_id = message.from_user.id
    if user_id != BOT_OWNER_ID:
        bot.reply_to(message, "❌ This command is owner-only.")
        return

    lines = []
    with jobs_lock:
        if not jobs:
            bot.reply_to(message, "ℹ️ No active jobs.")
            return
        for jid, j in jobs.items():
            status = "done" if j.get('done') else ("cancelled" if j.get('cancel_requested') else "running")
            started = j.get('started_at').strftime("%Y-%m-%d %H:%M:%S")
            lines.append(f"• `{jid}` — {status} — {j.get('url')} — started `{started}`")
    bot.reply_to(message, "🧾 Active jobs:\n" + "\n".join(lines))


@bot.message_handler(commands=["cancel"])
def cmd_cancel(message):
    user_id = message.from_user.id
    if user_id != BOT_OWNER_ID:
        bot.reply_to(message, "❌ This command is owner-only.")
        return
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "Usage: /cancel <job_id>")
        return
    jid = args[1].strip()
    with jobs_lock:
        job = jobs.get(jid)
        if not job:
            bot.reply_to(message, f"❌ Job `{jid}` not found.")
            return
        job['cancel_requested'] = True
    bot.reply_to(message, f"⚠️ Cancel requested for `{jid}`. Worker will stop/cleanup shortly.")


@bot.message_handler(commands=["clear"])
def cmd_clear(message):
    user_id = message.from_user.id
    if user_id != BOT_OWNER_ID:
        bot.reply_to(message, "❌ This command is owner-only.")
        return
    # Clear old download folders older than CLEANUP_AGE_HOURS
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
    bot.reply_to(message, f"🧹 Cleared {len(removed)} old download folder(s).")


# ------------------- Message handler for Mega links -------------------

@bot.message_handler(func=lambda m: isinstance(m.text, str) and is_mega_link(m.text.strip()))
def handle_mega_message(message):
    url = message.text.strip()
    chat_id = message.chat.id
    user_id = message.from_user.id

    # Create job
    job_id = str(uuid.uuid4())[:8]
    download_dir = os.path.join(DOWNLOAD_DIR, f"user_{user_id}_{job_id}")
    os.makedirs(download_dir, exist_ok=True)
    status_msg = bot.send_message(chat_id, f"🔗 Queued job `{job_id}`\nProcessing link...\n`{url}`")

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

    # Start monitor thread
    monitor_t = threading.Thread(target=monitor_download_progress, args=(job_id,), daemon=True)
    monitor_t.start()

    # Start download/upload worker thread
    worker_t = threading.Thread(target=download_and_handle, args=(job_id,), daemon=True)
    worker_t.start()

    bot.send_message(chat_id, f"🔔 Job started: `{job_id}`. I'll update this message as I download & upload.")


@bot.message_handler(func=lambda m: isinstance(m.text, str) and not is_mega_link(m.text.strip()))
def handle_other_text(message):
    # Non-mega text: reply basic help
    bot.reply_to(message, "Send a public Mega.nz file or folder link (e.g. `https://mega.nz/folder/XXXXX#KEY`) to start.")


# ------------------- Run -------------------
if __name__ == "__main__":
    logger.info("🚀 Advanced Mega.nz bot starting...")
    try:
        bot.infinity_polling()
    except KeyboardInterrupt:
        logger.info("Stopping by user request.")
