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

# Optional: requests + requests-toolbelt for upload progress
try:
    import requests
    from requests_toolbelt.multipart.encoder import MultipartEncoder, MultipartEncoderMonitor
    TOOLBELT_AVAILABLE = True
except Exception:
    requests = None
    MultipartEncoder = None
    MultipartEncoderMonitor = None
    TOOLBELT_AVAILABLE = False

# ------------------- Env variables (no dotenv) -------------------
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
BOT_OWNER_ID = int(os.environ.get("BOT_OWNER_ID", "0"))

# ------------------- Config -------------------
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/data/downloads")
TELEGRAM_MAX_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB safety
DOWNLOAD_POLL_INTERVAL = 1.0  # seconds for download monitor sampling
UPLOAD_PROGRESS_UPDATE_INTERVAL = 1.0  # seconds between upload progress message edits
CLEANUP_AGE_HOURS = 6
PROGRESS_BAR_LENGTH = 20

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

# ------------------- Download monitor (Option B: download progress first) -------------------

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

def make_progress_bar(pct, length=PROGRESS_BAR_LENGTH):
    filled = int(round(length * pct / 100.0))
    empty = length - filled
    return "▓" * filled + "░" * empty

def monitor_download_progress(job_id):
    """
    Periodically samples the download dir size and updates message with:
    - downloaded bytes so far
    - instantaneous speed (based on small window)
    - (ETA not provided because total unknown until download complete)
    """
    window = []  # list of (timestamp, bytes)
    window_seconds = 6.0  # how many seconds to average over
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

            now = time.time()
            size_now = get_total_size_of_path(download_dir)
            # append to window
            window.append((now, size_now))
            # remove old samples
            while window and (now - window[0][0]) > window_seconds:
                window.pop(0)
            # compute speed over window
            if len(window) >= 2:
                t0, b0 = window[0]
                t1, b1 = window[-1]
                delta_b = max(0, b1 - b0)
                delta_t = max(0.001, t1 - t0)
                speed = delta_b / delta_t  # bytes/sec
            else:
                speed = 0.0
            speed_h = human_size(speed) + "/s" if speed > 0 else "0 B/s"
            # percent impossible to compute reliably (no total), show downloaded bytes & speed
            text = (
                f"🔗 Processing Mega link:\n`{job['url']}`\n\n"
                f"⏳ Started: `{job['started_at'].strftime('%Y-%m-%d %H:%M:%S')}`\n"
                f"📥 Downloaded: `{human_size(size_now)}`\n"
                f"⚡ Speed: `{speed_h}`\n"
                f"_ETA: — (calculating until download finishes)_\n\n"
                f"_Updating every {DOWNLOAD_POLL_INTERVAL}s..._"
            )
            safe_edit_message(status_msg, text)
            if done:
                return
            time.sleep(DOWNLOAD_POLL_INTERVAL)
    except Exception as e:
        logger.exception(f"monitor_download_progress error for {job_id}: {e}")

# ------------------- Core worker: download then upload -------------------

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
        # blocking download call — files will be placed under download_dir
        m.download(url, path=download_dir)

        # Gather files after download finishes
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

        # Show download complete header with sizes
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

        # If too large for Telegram, skip upload and show path
        if total_bytes > TELEGRAM_MAX_BYTES:
            safe_edit_message(status_msg, header + f"\n\n⚠️ Total size exceeds Telegram limit ({human_size(TELEGRAM_MAX_BYTES)}). Upload skipped.\nLocal path: `{download_dir}`")
            return

        # Upload each file sequentially (show upload progress per file)
        for idx, p in enumerate(files, start=1):
            if job.get('cancel_requested'):
                safe_edit_message(status_msg, "❌ Upload cancelled by user. Cleaning up...")
                cleanup_dir(download_dir)
                return
            fname = os.path.basename(p)
            safe_edit_message(status_msg, f"📤 Preparing upload {idx}/{len(files)}: `{fname}`\nSize: `{human_size(os.path.getsize(p))}`")
            try:
                send_file_with_progress(chat_id, p, fname, status_msg)
            except Exception as e:
                logger.exception(f"Upload failed for {p}: {e}")
                bot.send_message(chat_id, f"⚠️ Failed to upload `{fname}`: {e}")

        safe_edit_message(status_msg, f"✅ All uploads complete. Sent {len(files)} file(s).")
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
        # cleanup
        try:
            if os.path.exists(download_dir):
                cleanup_dir(download_dir)
        except Exception as e:
            logger.debug(f"Final cleanup error: {e}")

# ------------------- Upload with progress (detailed) -------------------

def send_file_with_progress(chat_id, file_path, filename, status_msg):
    ext = os.path.splitext(filename)[1].lower()
    filesize = os.path.getsize(file_path)
    caption = f"📄 `{filename}`\nSize: {human_size(filesize)}"

    # decide API method and field
    if ext in [".mp4", ".mkv", ".mov", ".avi", ".webm"]:
        api_method = "sendVideo"
        file_field = "video"
        extra_fields = {"supports_streaming": "true"}
        allow_has_spoiler = True
    elif ext in [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"]:
        api_method = "sendPhoto"
        file_field = "photo"
        extra_fields = {}
        allow_has_spoiler = False
    elif ext in [".mp3", ".wav", ".m4a", ".ogg", ".flac"]:
        api_method = "sendAudio"
        file_field = "audio"
        extra_fields = {}
        allow_has_spoiler = False
    else:
        api_method = "sendDocument"
        file_field = "document"
        extra_fields = {}
        allow_has_spoiler = False

    # If requests-toolbelt available, do streaming multipart with monitor
    if TOOLBELT_AVAILABLE and requests is not None:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/{api_method}"
        fields = {
            "chat_id": str(chat_id),
            "caption": caption,
            **extra_fields
        }
        # include has_spoiler only for video if desired
        if allow_has_spoiler and api_method == "sendVideo":
            fields["has_spoiler"] = "true"
        f = open(file_path, "rb")
        fields[file_field] = (filename, f, "application/octet-stream")
        encoder = MultipartEncoder(fields=fields)
        start_time = time.time()
        last_update = 0.0

        def monitor_callback(monitor):
            nonlocal last_update, start_time
            now = time.time()
            if now - last_update < UPLOAD_PROGRESS_UPDATE_INTERVAL:
                return
            last_update = now
            uploaded = monitor.bytes_read
            pct = (uploaded / filesize) * 100 if filesize > 0 else 0.0
            elapsed = max(0.0001, now - start_time)
            speed = uploaded / elapsed  # bytes/sec
            speed_h = human_size(speed) + "/s"
            eta = (filesize - uploaded) / speed if speed > 0 else 0
            eta_str = time.strftime("%H:%M:%S", time.gmtime(eta)) if eta > 0 else "00:00:00"
            bar = make_progress_bar(pct)
            text = (
                f"📤 Uploading `{filename}`\n\n"
                f"Progress: {pct:.1f}% `{bar}`\n"
                f"Speed: {speed_h} | ETA: `{eta_str}`\n\n"
                f"_Uploading..._"
            )
            safe_edit_message(status_msg, text)

        monitor = MultipartEncoderMonitor(encoder, monitor_callback)
        headers = {"Content-Type": monitor.content_type}
        try:
            resp = requests.post(url, data=monitor, headers=headers, timeout=3600)
            f.close()
            if resp.status_code != 200:
                logger.error(f"Telegram upload failed: {resp.status_code} {resp.text}")
                # fallback to telebot send without progress
                send_file_without_progress(chat_id, file_path, filename, caption, api_method, allow_has_spoiler)
            else:
                # final update: compute final stats
                now = time.time()
                elapsed = max(0.0001, now - start_time)
                speed = filesize / elapsed
                speed_h = human_size(speed) + "/s"
                time_str = time.strftime("%H:%M:%S", time.gmtime(elapsed))
                bar = make_progress_bar(100)
                final_text = (
                    f"✅ Upload complete: `{filename}`\n\n"
                    f"Progress: 100% `{bar}`\n"
                    f"Avg speed: {speed_h} | Time: `{time_str}`"
                )
                safe_edit_message(status_msg, final_text)
        except Exception as e:
            logger.exception(f"Upload request error for {filename}: {e}")
            try:
                f.close()
            except Exception:
                pass
            send_file_without_progress(chat_id, file_path, filename, caption, api_method, allow_has_spoiler)
    else:
        # fallback: telebot send without live progress
        send_file_without_progress(chat_id, file_path, filename, caption, api_method, allow_has_spoiler)

def send_file_without_progress(chat_id, file_path, filename, caption, api_method, allow_has_spoiler):
    with open(file_path, "rb") as f:
        try:
            if api_method == "sendVideo":
                try:
                    bot.send_video(chat_id, f, caption=caption, supports_streaming=True, has_spoiler=True)
                except TypeError:
                    f.seek(0)
                    bot.send_video(chat_id, f, caption=caption, supports_streaming=True)
            elif api_method == "sendPhoto":
                bot.send_photo(chat_id, f, caption=caption)
            elif api_method == "sendAudio":
                bot.send_audio(chat_id, f, caption=caption)
            else:
                try:
                    bot.send_document(chat_id, f, caption=caption)
                except TypeError:
                    f.seek(0)
                    bot.send_document(chat_id, f, caption=caption)
        except Exception as e:
            raise

# ------------------- Bot commands -------------------

@bot.message_handler(commands=["start"])
def start(message):
    bot.reply_to(message,
        "👋 *Mega.nz Advanced Bot*\nSend a public `https://mega.nz/file/...` or `https://mega.nz/folder/...` link and I'll auto-download + upload.\nOwner commands: `/status`, `/cancel <job_id>`, `/clear`",
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
            state = "✅ done" if j.get('done') else ("⚠ cancelled" if j.get('cancel_requested') else "⏳ running")
            started = j.get('started_at').strftime("%Y-%m-%d %H:%M:%S")
            lines.append(f"`{jid}` — {state} — {j.get('url')} — started `{started}`")
    bot.reply_to(message, "🧾 Jobs:\n" + "\n".join(lines))

@bot.message_handler(commands=["cancel"])
def cancel(message):
    if message.from_user.id != BOT_OWNER_ID:
        bot.reply_to(message, "❌ Owner only.")
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
    bot.reply_to(message, f"🧹 Cleared {len(removed)} old download folder(s).")

# ------------------- Mega link handler (auto-detect) -------------------

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

    # start monitor then worker
    threading.Thread(target=monitor_download_progress, args=(job_id,), daemon=True).start()
    threading.Thread(target=download_and_handle, args=(job_id,), daemon=True).start()

@bot.message_handler(func=lambda m: True)
def other(message):
    bot.reply_to(message, "⚡ Send a public Mega.nz file or folder link to start.")

# ------------------- Run -------------------
if __name__ == "__main__":
    logger.info("🚀 Advanced Mega.nz bot (Option B: sequential progress) starting...")
    if not TOOLBELT_AVAILABLE:
        logger.warning("requests-toolbelt not available — upload progress will fallback. Install `requests-toolbelt` for live upload progress.")
    try:
        bot.infinity_polling()
    except KeyboardInterrupt:
        logger.info("Stopping by user request.")
