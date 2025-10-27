#!/usr/bin/env python3
"""
Per-file streaming Mega.nz -> Telegram bot (pymegatools).
Behavior:
 - For folder links: process file-by-file:
    * detect new file appearing in download dir,
    * wait until file is stable (size unchanged for STABLE_SECONDS),
    * upload that file to Telegram with live upload progress,
    * delete the file and continue to next.
 - For single-file links: download then upload normally (same per-file flow).
 - Same-message editable progress for download (per-file) and upload (per-file).
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

from pymegatools import Megatools
from pymegatools.pymegatools import MegaError

# Optional niceties
try:
    import humanize
    _HUMANIZE = True
except Exception:
    humanize = None
    _HUMANIZE = False

# requests-toolbelt for upload progress
try:
    import requests
    from requests_toolbelt.multipart.encoder import MultipartEncoder, MultipartEncoderMonitor
    TOOLBELT_AVAILABLE = True
except Exception:
    requests = None
    MultipartEncoder = None
    MultipartEncoderMonitor = None
    TOOLBELT_AVAILABLE = False

# -------------------- CONFIG (env) --------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
BOT_OWNER_ID = int(os.environ.get("BOT_OWNER_ID", "0"))
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", "/data/downloads")

TELEGRAM_MAX_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB safety
DOWNLOAD_POLL_INTERVAL = float(os.environ.get("DOWNLOAD_POLL_INTERVAL", "1.0"))
UPLOAD_PROGRESS_UPDATE_INTERVAL = float(os.environ.get("UPLOAD_PROGRESS_UPDATE_INTERVAL", "1.0"))
CLEANUP_AGE_HOURS = int(os.environ.get("CLEANUP_AGE_HOURS", "6"))
PROGRESS_BAR_LEN = int(os.environ.get("PROGRESS_BAR_LEN", "24"))
STABLE_SECONDS = float(os.environ.get("STABLE_SECONDS", "3.0"))  # consider file finished if size unchanged
MEGATOOLS_RETRY = int(os.environ.get("MEGATOOLS_RETRY", "3"))

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN environment variable is required")

# -------------------- SETUP --------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("mega_stream_bot")
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

jobs = {}        # job_id -> dict
jobs_lock = threading.Lock()

MEGA_RE = re.compile(r"https://mega\.nz/(file|folder)/[A-Za-z0-9_-]+#[A-Za-z0-9_-]+")

# -------------------- HELPER FUNCTIONS --------------------
def human_size(n):
    if _HUMANIZE:
        return humanize.naturalsize(n, binary=True)
    try:
        n = float(n)
        if n < 1024:
            return f"{n:.0f} B"
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
        if "message not modified" not in str(e).lower():
            logger.debug(f"safe_edit error: {e}")

def ffprobe_duration(path):
    try:
        proc = subprocess.run(
            ["ffprobe","-v","quiet","-print_format","json","-show_format", path],
            capture_output=True, text=True, timeout=15
        )
        if proc.returncode == 0 and proc.stdout:
            import json
            data = json.loads(proc.stdout)
            dur = float(data.get("format", {}).get("duration", 0) or 0)
            return int(dur)
    except Exception:
        pass
    return 0

def format_hms(seconds):
    try:
        seconds = int(round(seconds))
        return time.strftime("%H:%M:%S", time.gmtime(seconds))
    except Exception:
        return "00:00:00"

def safe_remove(path):
    try:
        if os.path.exists(path):
            if os.path.isfile(path):
                os.remove(path)
            else:
                shutil.rmtree(path)
    except Exception as e:
        logger.debug(f"safe_remove failed: {e}")

def is_mega_link(text):
    return bool(MEGA_RE.search(text.strip()))

# -------------------- File stability detector --------------------
def wait_for_file_stable(path, stable_seconds=STABLE_SECONDS, poll=1.0, cancel_check=lambda: False):
    """
    Wait until file size hasn't changed for `stable_seconds`. Returns True when stable.
    If cancel_check() becomes True, returns False.
    """
    last_size = -1
    unchanged_since = None
    while True:
        if cancel_check():
            return False
        if not os.path.exists(path):
            time.sleep(poll)
            continue
        try:
            size = os.path.getsize(path)
        except Exception:
            size = -1
        now = time.time()
        if size == last_size:
            if unchanged_since is None:
                unchanged_since = now
            elif now - unchanged_since >= stable_seconds:
                return True
        else:
            last_size = size
            unchanged_since = None
        time.sleep(poll)

# -------------------- Per-file upload (requests-toolbelt monitor) --------------------
def upload_with_progress(chat_id, file_path, status_msg):
    """
    Upload single file with live progress edits in `status_msg`.
    Uses requests-toolbelt if available else fallback to telebot (no live progress).
    """
    fname = os.path.basename(file_path)
    fsize = os.path.getsize(file_path)
    ext = os.path.splitext(fname)[1].lower()

    # map to API method & field
    if ext in [".mp4", ".mkv", ".mov", ".avi", ".webm"]:
        api_method = "sendVideo"; file_field = "video"; extra = {"supports_streaming":"true"}; allow_spoiler = True
    elif ext in [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"]:
        api_method = "sendPhoto"; file_field = "photo"; extra = {}; allow_spoiler = False
    elif ext in [".mp3", ".wav", ".m4a", ".ogg", ".flac"]:
        api_method = "sendAudio"; file_field = "audio"; extra = {}; allow_spoiler = False
    else:
        api_method = "sendDocument"; file_field = "document"; extra = {}; allow_spoiler = False

    if TOOLBELT_AVAILABLE and requests is not None:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/{api_method}"
        fields = {"chat_id": str(chat_id), "caption": f"{fname}\n{human_size(fsize)}"}
        fields.update(extra)
        if allow_spoiler and api_method == "sendVideo":
            fields["has_spoiler"] = "true"
        f = open(file_path, "rb")
        fields[file_field] = (fname, f, "application/octet-stream")
        encoder = MultipartEncoder(fields=fields)
        start = time.time()
        last_edit = 0.0

        def monitor_cb(monitor):
            nonlocal last_edit, start
            now = time.time()
            if now - last_edit < UPLOAD_PROGRESS_UPDATE_INTERVAL:
                return
            last_edit = now
            uploaded = monitor.bytes_read
            pct = (uploaded / fsize) * 100 if fsize else 0.0
            elapsed = max(0.0001, now - start)
            speed = uploaded / elapsed
            rem = max(0, fsize - uploaded)
            eta = rem / speed if speed > 0 else 0
            bar = make_progress_bar(pct)
            text = (
                f"📤 Uploading: `{fname}`\n\n"
                f"Progress: {pct:5.1f}% `{bar}`\n"
                f"⚡ Speed: `{human_size(int(speed))}/s` | ETA: `{format_hms(eta)}`\n"
            )
            safe_edit(status_msg, text)

        monitor = MultipartEncoderMonitor(encoder, monitor_cb)
        headers = {"Content-Type": monitor.content_type}
        try:
            resp = requests.post(url, data=monitor, headers=headers, timeout=3600)
            f.close()
            if resp.status_code != 200:
                logger.error(f"Upload failed: {resp.status_code} {resp.text}")
                # fallback to telebot send (no live progress)
                send_via_telebot_fallback(chat_id, file_path, status_msg, api_method, allow_spoiler)
            else:
                elapsed = time.time() - start
                avg = fsize / max(1e-6, elapsed)
                txt = (
                    f"✅ Upload complete: `{fname}`\n\n"
                    f"Progress: 100% `{make_progress_bar(100)}`\n"
                    f"Avg speed: `{human_size(int(avg))}/s` | Time: `{format_hms(elapsed)}`"
                )
                safe_edit(status_msg, txt)
        except Exception as e:
            logger.exception(f"Upload request error: {e}")
            try:
                f.close()
            except Exception:
                pass
            send_via_telebot_fallback(chat_id, file_path, status_msg, api_method, allow_spoiler)
    else:
        send_via_telebot_fallback(chat_id, file_path, status_msg, api_method, allow_spoiler)

def send_via_telebot_fallback(chat_id, file_path, status_msg, api_method, allow_spoiler):
    fname = os.path.basename(file_path)
    cap = f"{fname}\n{human_size(os.path.getsize(file_path))}"
    with open(file_path, "rb") as f:
        try:
            if api_method == "sendVideo":
                try:
                    bot.send_video(chat_id, f, caption=cap, supports_streaming=True, has_spoiler=True)
                except TypeError:
                    f.seek(0)
                    bot.send_video(chat_id, f, caption=cap, supports_streaming=True)
            elif api_method == "sendPhoto":
                bot.send_photo(chat_id, f, caption=cap)
            elif api_method == "sendAudio":
                bot.send_audio(chat_id, f, caption=cap)
            else:
                try:
                    bot.send_document(chat_id, f, caption=cap)
                except TypeError:
                    f.seek(0)
                    bot.send_document(chat_id, f, caption=cap)
            safe_edit(status_msg, f"✅ Uploaded: `{fname}`\nSize: `{human_size(os.path.getsize(file_path))}`")
        except Exception as e:
            logger.exception(f"telebot send error: {e}")
            safe_edit(status_msg, f"⚠️ Failed to upload `{fname}`: {e}")

# -------------------- File-by-file streaming worker --------------------
# Approach:
# 1) Ensure fresh download_dir (auto-overwrite) for the job.
# 2) Start a separate thread to run mega.download(url, path=download_dir) (blocking).
# 3) While download thread runs OR there are pending files in the dir, watch directory for new files.
# 4) When a new file appears and is stable, upload it, then delete it immediately.
# 5) After download thread finishes, process any remaining files similarly.

_EXIST_REGEX = re.compile(r"File already exists at (.+)")

def worker_stream(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return
    url = job["url"]
    chat_id = job["chat_id"]
    status_msg = job["status_msg"]
    download_dir = job["download_dir"]

    # prepare fresh dir (auto-overwrite behavior)
    try:
        if os.path.exists(download_dir):
            safe_remove(download_dir)
        os.makedirs(download_dir, exist_ok=True)
    except Exception as e:
        logger.debug(f"prepare dir error: {e}")

    mega = Megatools()

    # function to run download blocking
    def download_thread_fn():
        attempt = 0
        while attempt < MEGATOOLS_RETRY:
            attempt += 1
            try:
                mega.download(url, path=download_dir)
                return None
            except Exception as e:
                txt = str(e)
                m = _EXIST_REGEX.search(txt)
                if m:
                    path = m.group(1).strip()
                    try:
                        if os.path.exists(path):
                            safe_remove(path)
                            logger.info(f"Removed existing file to allow retry: {path}")
                    except Exception as rem_e:
                        logger.debug(f"Failed to remove existing file {path}: {rem_e}")
                    time.sleep(1)
                    continue
                else:
                    # unknown error -> return it
                    return txt
        return f"Download failed after {MEGATOOLS_RETRY} retries."

    dl_thread = threading.Thread(target=download_thread_fn, daemon=True)
    dl_thread.start()

    # track processed filenames to avoid double-processing
    processed = set()

    try:
        # main loop: while downloader running or new unprocessed files exist
        while dl_thread.is_alive() or True:
            # build list of candidate files
            current_files = []
            for root, _, files in os.walk(download_dir):
                for fn in files:
                    fp = os.path.join(root, fn)
                    if fp not in processed:
                        current_files.append(fp)

            if not current_files:
                if dl_thread.is_alive():
                    # nothing yet — wait a bit
                    time.sleep(0.8)
                    continue
                else:
                    # downloader finished and nothing new -> break
                    break

            # process each newly detected file one-by-one
            for fp in sorted(current_files):
                if fp in processed:
                    continue
                # wait until stable
                stable = wait_for_file_stable(fp, stable_seconds=STABLE_SECONDS,
                                              poll=0.8,
                                              cancel_check=lambda: job.get("cancel_requested", False))
                if not stable:
                    # cancelled or removed: skip
                    processed.add(fp)
                    continue

                # file is stable — mark processed
                processed.add(fp)

                # show download-complete for this single file
                try:
                    fsize = os.path.getsize(fp)
                except Exception:
                    fsize = 0
                txt = (
                    f"✅ Downloaded: `{os.path.basename(fp)}`\n"
                    f"📦 Size: `{human_size(fsize)}`\n"
                )
                # include duration if video
                ext = os.path.splitext(fp)[1].lower()
                if ext in [".mp4", ".mkv", ".mov", ".avi", ".webm"]:
                    dur = ffprobe_duration(fp)
                    if dur:
                        txt += f"⏱ Duration: `{format_hms(dur)}`\n"
                safe_edit(status_msg, txt)

                # If exceeds Telegram limit, notify and skip upload for that file
                if fsize > TELEGRAM_MAX_BYTES:
                    safe_edit(status_msg, txt + f"\n⚠️ File exceeds Telegram limit ({human_size(TELEGRAM_MAX_BYTES)}). Skipping upload.\nLocal path: `{fp}`")
                    # do not delete (user may want manual handling), but mark as processed
                    continue

                # upload this file
                try:
                    upload_with_progress(chat_id, fp, status_msg)
                except Exception as e:
                    logger.exception(f"Upload failed for {fp}: {e}")
                    bot.send_message(chat_id, f"⚠️ Upload failed for `{os.path.basename(fp)}`: {e}")

                # after upload, delete file to free space
                try:
                    if os.path.exists(fp):
                        os.remove(fp)
                except Exception as e:
                    logger.debug(f"Failed to remove file {fp}: {e}")

            # small sleep to avoid busy loop
            if dl_thread.is_alive():
                time.sleep(0.5)
            else:
                # check if any remaining unprocessed files (loop will catch them)
                time.sleep(0.5)

    finally:
        # mark job done
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id]["done"] = True
        # optionally remove download dir if empty
        try:
            if os.path.exists(download_dir) and not os.listdir(download_dir):
                safe_remove(download_dir)
                logger.info(f"Cleaned up: {download_dir}")
        except Exception:
            pass

# -------------------- Bot commands --------------------
@bot.message_handler(commands=["start"])
def cmd_start(m):
    bot.reply_to(m, "👋 Send a public Mega.nz file or folder link. Owner commands: /status /cancel <job_id> /clear", parse_mode="Markdown")

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
                safe_remove(p); removed += 1
        except Exception:
            pass
    bot.reply_to(m, f"🧹 Cleared {removed} old download folder(s).")

# -------------------- Mega link handler (auto) --------------------
@bot.message_handler(func=lambda m: isinstance(m.text, str) and is_mega_link(m.text.strip()))
def handle_link(m):
    url = m.text.strip()
    chat_id = m.chat.id
    user_id = m.from_user.id

    job_id = str(uuid.uuid4())[:8]
    download_dir = os.path.join(DOWNLOAD_DIR, f"user_{user_id}_{job_id}")
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

    threading.Thread(target=worker_stream, args=(job_id,), daemon=True).start()
    bot.send_message(chat_id, f"🔔 Job queued: `{job_id}` — I'll update this message as I download & upload.", parse_mode="Markdown")

@bot.message_handler(func=lambda m: True)
def fallback(m):
    bot.reply_to(m, "⚡ Send a public Mega.nz file or folder link to start.")

# -------------------- RUN --------------------
if __name__ == "__main__":
    logger.info("🚀 Mega streaming bot starting. TOOLBELT_AVAILABLE=%s", TOOLBELT_AVAILABLE)
    if not TOOLBELT_AVAILABLE:
        logger.warning("requests-toolbelt not available — upload progress will fallback.")
    try:
        bot.infinity_polling()
    except KeyboardInterrupt:
        logger.info("Stopping...")
