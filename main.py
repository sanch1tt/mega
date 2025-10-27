#!/usr/bin/env python3
"""
Pro+ Mega.nz -> Telegram bot (pymegatools). Option B: NO zipping, uploads files individually.

Features:
- auto-detect mega.nz links (file + folder)
- auto-overwrite existing downloads
- download progress monitor (same-message updates)
- upload progress (percentage, bar, speed, ETA) using requests-toolbelt (fallback available)
- file info: name, size, ffprobe duration (if ffprobe installed)
- owner-only commands: /status, /cancel <job_id>, /clear
- cleanup after upload
- no dotenv (reads os.environ)
"""

import os
import re
import time
import uuid
import math
import shutil
import threading
import logging
import subprocess
from datetime import datetime, timedelta

import telebot
from telebot import types

# pymegatools
from pymegatools import Megatools
from pymegatools.pymegatools import MegaError

# human-friendly sizes
try:
    import humanize
    humanize_available = True
except Exception:
    humanize_available = False

# requests-toolbelt for upload monitor
try:
    import requests
    from requests_toolbelt.multipart.encoder import MultipartEncoder, MultipartEncoderMonitor
    TOOLBELT_AVAILABLE = True
except Exception:
    requests = None
    MultipartEncoder = None
    MultipartEncoderMonitor = None
    TOOLBELT_AVAILABLE = False

# ---------------------- CONFIG (env) ----------------------
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
BOT_OWNER_ID = int(os.environ.get("BOT_OWNER_ID", "0"))

DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/data/downloads")
TELEGRAM_MAX_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB safe limit
DOWNLOAD_POLL_INTERVAL = float(os.environ.get("DOWNLOAD_POLL_INTERVAL", "1.0"))  # seconds
UPLOAD_PROGRESS_UPDATE_INTERVAL = float(os.environ.get("UPLOAD_PROGRESS_UPDATE_INTERVAL", "1.0"))
CLEANUP_AGE_HOURS = int(os.environ.get("CLEANUP_AGE_HOURS", "6"))
PROGRESS_BAR_LEN = int(os.environ.get("PROGRESS_BAR_LEN", "24"))

# ---------------------- SETUP ----------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("mega_pro_bot")
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Job store
jobs = {}  # job_id -> dict
jobs_lock = threading.Lock()

# ---------------------- UTILITIES ----------------------
def human_size(n):
    if humanize_available:
        return humanize.naturalsize(n, binary=True)
    try:
        # fallback
        if n < 1024:
            return f"{n} B"
        for unit in ("KB","MB","GB","TB"):
            n /= 1024.0
            if n < 1024.0:
                return f"{n:.2f} {unit}"
        return f"{n:.2f} PB"
    except Exception:
        return str(n)

def make_progress_bar(pct, length=PROGRESS_BAR_LEN):
    pct = max(0.0, min(100.0, pct))
    filled = int(round(length * pct / 100.0))
    return "▓" * filled + "░" * (length - filled)

def safe_edit(msg, text):
    try:
        bot.edit_message_text(text, msg.chat.id, msg.message_id, parse_mode="Markdown")
    except Exception as e:
        # ignore "message not modified" and ephemeral edit issues
        if "message not modified" not in str(e).lower():
            logger.debug(f"safe_edit error: {e}")

def ffprobe_duration(path):
    """Return duration in seconds if ffprobe exists and can read file, else 0."""
    try:
        proc = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", path],
                              capture_output=True, text=True, timeout=15)
        if proc.returncode == 0 and proc.stdout:
            import json
            data = json.loads(proc.stdout)
            dur = float(data.get("format", {}).get("duration", 0) or 0)
            return int(dur)
    except Exception:
        pass
    return 0

def format_duration(seconds):
    try:
        seconds = int(seconds)
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        if h:
            return f"{h:02d}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"
    except Exception:
        return "00:00"

def safe_remove(path):
    try:
        if os.path.exists(path):
            if os.path.isfile(path):
                os.remove(path)
            else:
                shutil.rmtree(path)
    except Exception as e:
        logger.debug(f"safe_remove error: {e}")

# ---------------------- MEGA LINK CHECK ----------------------
MEGA_LINK_RE = re.compile(r"https://mega\.nz/(file|folder)/[A-Za-z0-9_-]+#[A-Za-z0-9_-]+")
def is_mega_link(url):
    return bool(MEGA_LINK_RE.search(url.strip()))

# ---------------------- DOWNLOAD MONITOR ----------------------
def get_total_bytes(path):
    total = 0
    if not os.path.exists(path):
        return 0
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except:
            return 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except Exception:
                pass
    return total

def monitor_download(job_id):
    """Periodically sample directory bytes and update status message with downloaded bytes and speed.
       ETA for download is not reliable because remote total unknown; we show downloaded & speed."""
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return
    status_msg = job["status_msg"]
    download_dir = job["download_dir"]
    samples = []  # (t, bytes)
    window = 6.0
    try:
        while True:
            with jobs_lock:
                job = jobs.get(job_id)
                if not job:
                    return
                if job.get("cancel_requested"):
                    safe_edit(status_msg, f"❌ Download cancelled by user.")
                    return
                done = job.get("done", False)
            now = time.time()
            b = get_total_bytes(download_dir)
            samples.append((now, b))
            # purge old
            while samples and (now - samples[0][0]) > window:
                samples.pop(0)
            # compute speed
            if len(samples) >= 2:
                t0, b0 = samples[0]
                t1, b1 = samples[-1]
                speed = (b1 - b0) / max(0.0001, (t1 - t0))
            else:
                speed = 0.0
            text = (
                f"🔗 Downloading: `{job['url']}`\n\n"
                f"📥 Downloaded: `{human_size(b)}`\n"
                f"⚡ Speed: `{human_size(int(speed))}/s`\n"
                f"_ETA: — (will show after download completes)_\n\n"
                f"_Updating every {DOWNLOAD_POLL_INTERVAL}s..._"
            )
            safe_edit(status_msg, text)
            if done:
                return
            time.sleep(DOWNLOAD_POLL_INTERVAL)
    except Exception as e:
        logger.exception(f"monitor_download error: {e}")

# ---------------------- UPLOAD WITH PROGRESS ----------------------
def send_file_via_telegraph_api_with_progress(chat_id, file_path, status_msg):
    """Uploads using Telegram Bot API via requests + MultipartEncoderMonitor to provide progress updates."""
    filename = os.path.basename(file_path)
    filesize = os.path.getsize(file_path)
    start = time.time()

    # choose method based on ext
    ext = os.path.splitext(filename)[1].lower()
    if ext in [".mp4", ".mkv", ".mov", ".avi", ".webm"]:
        api_method = "sendVideo"
        file_field = "video"
        extra = {"supports_streaming": "true"}
        try_has_spoiler = True
    elif ext in [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"]:
        api_method = "sendPhoto"
        file_field = "photo"
        extra = {}
        try_has_spoiler = False
    elif ext in [".mp3", ".wav", ".m4a", ".ogg", ".flac"]:
        api_method = "sendAudio"
        file_field = "audio"
        extra = {}
        try_has_spoiler = False
    else:
        api_method = "sendDocument"
        file_field = "document"
        extra = {}
        try_has_spoiler = False

    if TOOLBELT_AVAILABLE and requests is not None:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/{api_method}"
        fields = {
            "chat_id": str(chat_id),
            "caption": f"{filename}\n{human_size(filesize)}"
        }
        fields.update(extra)
        if try_has_spoiler and api_method == "sendVideo":
            fields["has_spoiler"] = "true"

        f = open(file_path, "rb")
        fields[file_field] = (filename, f, "application/octet-stream")
        encoder = MultipartEncoder(fields=fields)
        last_edit = 0.0

        def _monitor(monitor):
            nonlocal last_edit, start
            now = time.time()
            if now - last_edit < UPLOAD_PROGRESS_UPDATE_INTERVAL:
                return
            last_edit = now
            uploaded = monitor.bytes_read
            pct = (uploaded / filesize) * 100 if filesize > 0 else 0.0
            elapsed = max(0.0001, now - start)
            speed = uploaded / elapsed
            rem = max(0, filesize - uploaded)
            eta = rem / speed if speed > 0 else 0
            bar = make_progress_bar(pct)
            txt = (
                f"📤 Uploading: `{filename}`\n\n"
                f"Progress: {pct:5.1f}% `{bar}`\n"
                f"⚡ Speed: `{human_size(int(speed))}/s` | ETA: `{format_eta(eta)}`\n"
            )
            safe_edit(status_msg, txt)

        monitor = MultipartEncoderMonitor(encoder, _monitor)
        headers = {"Content-Type": monitor.content_type}
        try:
            resp = requests.post(url, data=monitor, headers=headers, timeout=3600)
            f.close()
            if resp.status_code != 200:
                logger.error(f"Upload failed: {resp.status_code} {resp.text}")
                # fallback to telebot send without progress
                send_file_without_progress(chat_id, file_path, status_msg, api_method, try_has_spoiler)
            else:
                # final update
                elapsed = time.time() - start
                avg_speed = filesize / max(1e-6, elapsed)
                txt = (
                    f"✅ Upload complete: `{filename}`\n\n"
                    f"Progress: 100% `{make_progress_bar(100)}`\n"
                    f"Avg speed: `{human_size(int(avg_speed))}/s` | Time: `{format_duration(elapsed)}`"
                )
                safe_edit(status_msg, txt)
        except Exception as e:
            logger.exception(f"Upload request error: {e}")
            try:
                f.close()
            except Exception:
                pass
            send_file_without_progress(chat_id, file_path, status_msg, api_method, try_has_spoiler)
    else:
        # fallback: telebot without progress
        send_file_without_progress(chat_id, file_path, status_msg, api_method, try_has_spoiler)

def send_file_without_progress(chat_id, file_path, status_msg, api_method, try_has_spoiler):
    """Fallback using telebot methods with basic caption."""
    filename = os.path.basename(file_path)
    caption = f"{filename}\n{human_size(os.path.getsize(file_path))}"
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
            safe_edit(status_msg, f"✅ Uploaded: `{filename}`\nSize: `{human_size(os.path.getsize(file_path))}`")
        except Exception as e:
            logger.exception(f"telebot upload error: {e}")
            safe_edit(status_msg, f"⚠️ Failed to upload `{filename}`: {e}")

def format_eta(seconds):
    try:
        seconds = int(round(seconds))
        return time.strftime("%H:%M:%S", time.gmtime(seconds))
    except Exception:
        return "00:00:00"

def format_duration(sec):
    try:
        sec = int(round(sec))
        m, s = divmod(sec, 60)
        h, m = divmod(m, 60)
        if h:
            return f"{h:02d}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"
    except Exception:
        return "00:00"

# ---------------------- WORKER: download -> upload ----------------------
def worker_job(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return
    url = job["url"]
    chat_id = job["chat_id"]
    status_msg = job["status_msg"]
    download_dir = job["download_dir"]

    # Overwrite policy: if folder exists, remove it (auto-overwrite)
    try:
        if os.path.exists(download_dir):
            # remove existing folder to avoid "file exists" issues
            safe_remove(download_dir)
        os.makedirs(download_dir, exist_ok=True)
    except Exception as e:
        logger.debug(f"prepare download dir error: {e}")

    mega = Megatools()
    try:
        safe_edit(status_msg, "⬇️ Download starting...")
        # Start monitor thread for live download progress (samples dir size)
        monitor_t = threading.Thread(target=monitor_download, args=(job_id,), daemon=True)
        monitor_t.start()

        # Download (blocking)
        mega.download(url, path=download_dir)

        # After download completes, collect files
        files = []
        for root, _, files_list in os.walk(download_dir):
            for fn in files_list:
                files.append(os.path.join(root, fn))

        with jobs_lock:
            job["files"] = files
            job["done"] = True

        if job.get("cancel_requested"):
            safe_edit(status_msg, "❌ Cancelled during download. Cleaning up...")
            safe_remove(download_dir)
            return

        if not files:
            safe_edit(status_msg, "⚠️ Download finished but no files found.")
            safe_remove(download_dir)
            return

        # Prepare summary and file info (with duration for video if possible)
        info_lines = []
        total_bytes = 0
        for p in files:
            s = os.path.getsize(p) if os.path.exists(p) else 0
            total_bytes += s
            ext = os.path.splitext(p)[1].lower()
            dur = 0
            if ext in [".mp4", ".mkv", ".mov", ".avi", ".webm"]:
                dur = ffprobe_duration(p)
            info_lines.append(f"• `{os.path.basename(p)}` — {human_size(s)}" + (f" — {format_duration(dur)}" if dur else ""))

        header = (
            f"✅ Download complete!\n\n"
            f"📄 Files ({len(files)}):\n" + "\n".join(info_lines) + f"\n\nTotal: `{human_size(total_bytes)}`"
        )
        safe_edit(status_msg, header)

        # Skip upload if larger than Telegram limit
        if total_bytes > TELEGRAM_MAX_BYTES:
            safe_edit(status_msg, header + f"\n\n⚠️ Total exceeds Telegram limit ({human_size(TELEGRAM_MAX_BYTES)}). Upload skipped.\nLocal path: `{download_dir}`")
            return

        # Now upload sequentially, with per-file progress message updates
        for idx, p in enumerate(files, start=1):
            if job.get("cancel_requested"):
                safe_edit(status_msg, "❌ Cancelled by user. Cleaning up...")
                safe_remove(download_dir)
                return
            fname = os.path.basename(p)
            safe_edit(status_msg, f"📤 Uploading {idx}/{len(files)}: `{fname}`\nSize: `{human_size(os.path.getsize(p))}`")
            # upload with progress
            send_file_via_telegraph_api_with_progress(chat_id, p, status_msg)
            # delete file after upload
            try:
                os.remove(p)
            except Exception:
                pass

        safe_edit(status_msg, f"✅ All uploads done. Sent {len(files)} file(s).")
    except MegaError as me:
        logger.exception("Megatools error", exc_info=True)
        safe_edit(status_msg, f"❌ Mega.nz Error: `{me}`")
    except Exception as e:
        logger.exception("Worker error", exc_info=True)
        safe_edit(status_msg, f"❌ Unexpected error: `{e}`")
    finally:
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id]["done"] = True
        # cleanup folder if empty
        try:
            if os.path.exists(download_dir) and not os.listdir(download_dir):
                safe_remove(download_dir)
        except Exception:
            pass

# ---------------------- COMMANDS ----------------------
@bot.message_handler(commands=["start"])
def cmd_start(m):
    bot.reply_to(m, "👋 Send a public `https://mega.nz/file/...` or `https://mega.nz/folder/...` link. Owner commands: /status /cancel <job_id> /clear", parse_mode="Markdown")

@bot.message_handler(commands=["status"])
def cmd_status(m):
    if m.from_user.id != BOT_OWNER_ID:
        bot.reply_to(m, "❌ Owner only.")
        return
    with jobs_lock:
        if not jobs:
            bot.reply_to(m, "ℹ️ No active jobs.")
            return
        lines = []
        for jid, j in jobs.items():
            st = "✅ done" if j.get("done") else ("⚠ cancelled" if j.get("cancel_requested") else "⏳ running")
            started = j.get("started_at").strftime("%Y-%m-%d %H:%M:%S")
            lines.append(f"`{jid}` — {st} — {j.get('url')} — started `{started}`")
    bot.reply_to(m, "🧾 Jobs:\n" + "\n".join(lines), parse_mode="Markdown")

@bot.message_handler(commands=["cancel"])
def cmd_cancel(m):
    if m.from_user.id != BOT_OWNER_ID:
        bot.reply_to(m, "❌ Owner only.")
        return
    parts = m.text.split()
    if len(parts) < 2:
        bot.reply_to(m, "Usage: /cancel <job_id>")
        return
    jid = parts[1].strip()
    with jobs_lock:
        job = jobs.get(jid)
        if not job:
            bot.reply_to(m, f"❌ Job `{jid}` not found.", parse_mode="Markdown")
            return
        job["cancel_requested"] = True
    bot.reply_to(m, f"⚠️ Cancel requested for `{jid}`.", parse_mode="Markdown")

@bot.message_handler(commands=["clear"])
def cmd_clear(m):
    if m.from_user.id != BOT_OWNER_ID:
        bot.reply_to(m, "❌ Owner only.")
        return
    cutoff = datetime.utcnow() - timedelta(hours=CLEANUP_AGE_HOURS)
    removed = 0
    for entry in os.listdir(DOWNLOAD_DIR):
        p = os.path.join(DOWNLOAD_DIR, entry)
        try:
            mtime = datetime.utcfromtimestamp(os.path.getmtime(p))
            if mtime < cutoff:
                safe_remove(p)
                removed += 1
        except Exception:
            pass
    bot.reply_to(m, f"🧹 Cleared {removed} old download folder(s).")

# ---------------------- AUTO-DETECT MEGA LINKS ----------------------
@bot.message_handler(func=lambda m: isinstance(m.text, str) and is_mega_link(m.text.strip()))
def handle_message(m):
    url = m.text.strip()
    chat_id = m.chat.id
    user_id = m.from_user.id

    job_id = str(uuid.uuid4())[:8]
    download_dir = os.path.join(DOWNLOAD_DIR, f"user_{user_id}_{job_id}")

    # Create status message
    status_msg = bot.send_message(chat_id, f"🔗 Job `{job_id}` started\nProcessing `{url}`", parse_mode="Markdown")

    job = {
        "user_id": user_id,
        "chat_id": chat_id,
        "url": url,
        "status_msg": status_msg,
        "download_dir": download_dir,
        "files": [],
        "started_at": datetime.utcnow(),
        "cancel_requested": False,
        "done": False,
    }

    with jobs_lock:
        jobs[job_id] = job

    # Start worker thread
    t = threading.Thread(target=worker_job, args=(job_id,), daemon=True)
    t.start()

    bot.send_message(chat_id, f"🔔 Job queued: `{job_id}` — I'll update this message as I download & upload.", parse_mode="Markdown")

@bot.message_handler(func=lambda m: True)
def fallback(m):
    bot.reply_to(m, "⚡ Send a public Mega.nz file or folder link to start.")

# ---------------------- RUN ----------------------
if __name__ == "__main__":
    logger.info("🚀 Pro+ Mega.nz bot starting (pymegatools). TOOLBELT_AVAILABLE=%s", TOOLBELT_AVAILABLE)
    if not TOOLBELT_AVAILABLE:
        logger.warning("requests-toolbelt not available — upload progress will fallback. Install `requests-toolbelt` for live upload progress.")
    try:
        bot.infinity_polling()
    except KeyboardInterrupt:
        logger.info("Stopping...")
