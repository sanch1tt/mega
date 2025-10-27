import os
import humanize
import logging
import re
import shutil
import uuid
import time
import subprocess
import json
from datetime import datetime
import telebot
from telebot.types import InputFile
from pymegatools import Megatools
from pymegatools.pymegatools import MegaError

# 🔹 Configuration
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
BOT_OWNER_ID = int(os.environ.get("BOT_OWNER_ID"))

DOWNLOAD_DIR = "/data/downloads"

# 🔹 Logging Setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# 🔹 Initialize Bot & Megatools
try:
    bot = telebot.TeleBot(BOT_TOKEN, parse_mode='Markdown')
    mega = Megatools()
    logger.info("✅ Bot initialized with pyTelegramBotAPI")
except Exception as e:
    logger.critical(f"❌ Failed to initialize bot or Megatools: {e}")
    exit(1)

# --- Helper Functions ---

def progress_bar(percentage, bar_length=20):
    filled = int(bar_length * percentage / 100)
    empty = bar_length - filled
    return "█" * filled + "░" * empty

def edit_message(message, text):
    try:
        bot.edit_message_text(text, message.chat.id, message.message_id)
    except Exception as e:
        if "message not modified" not in str(e).lower():
            logger.error(f"Error editing message: {str(e)}")

def delete_message(message, delay=5):
    try:
        if delay > 0:
            time.sleep(delay)
        bot.delete_message(message.chat.id, message.message_id)
    except Exception as e:
        logger.error(f"Error deleting message: {str(e)}")

def clean_directory(directory):
    try:
        if os.path.exists(directory):
            shutil.rmtree(directory)
            logger.info(f"🪥 Cleaned directory: {directory}")
            return True
        else:
            logger.warning(f"⚠️ Directory not found: {directory}")
            return False
    except Exception as e:
        logger.error(f"❌ Error cleaning directory {directory}: {repr(e)}")
        return False

def get_video_duration(file_path):
    try:
        command = [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", file_path
        ]
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        if result.returncode == 0:
            data = json.loads(result.stdout)
            return int(float(data['format']['duration']))
        else:
            logger.warning(f"ffprobe failed: {result.stderr}")
            return 0
    except FileNotFoundError:
        logger.warning("ffprobe not found. Skipping video duration.")
        return 0
    except Exception as e:
        logger.error(f"Error in get_video_duration: {e}")
        return 0

# --- Upload File ---

def upload_file(chat_id, status_msg, file_path, filename, file_index, total_files):
    try:
        size_bytes = os.path.getsize(file_path)
        size_human = humanize.naturalsize(size_bytes, binary=True)
        ext = os.path.splitext(filename)[1].lower() or ".file"

        caption = (
            f"📂 **File:** `{filename}`\n"
            f"📦 **Size:** `{size_human}`\n"
            f"📄 **Extension:** `{ext}`"
        )

        edit_message(status_msg, f"📤 **Uploading file {file_index}/{total_files}:**\n`{filename}`")

        with open(file_path, 'rb') as f:
            # ✅ Smart type detection
            if ext in [".mp4", ".mkv", ".mov", ".avi", ".webm"]:
                duration = get_video_duration(file_path)
                bot.send_video(
                    chat_id,
                    f,
                    caption=caption,
                    duration=duration,
                    supports_streaming=True,
                    has_spoiler=True
                )

            elif ext in [".mp3", ".wav", ".m4a", ".ogg", ".flac"]:
                bot.send_audio(chat_id, f, caption=caption)

            elif ext in [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"]:
                bot.send_photo(chat_id, f, caption=caption)

            elif ext in [".pdf", ".zip", ".rar", ".7z", ".tar", ".gz", ".txt", ".doc", ".docx", ".pptx", ".xlsx"]:
                bot.send_document(chat_id, f, caption=caption)

            else:
                # Default fallback for unknown types
                bot.send_document(chat_id, f, caption=caption)

        logger.info(f"✅ Uploaded {filename}")
        return True

    except Exception as e:
        logger.error(f"❌ Failed to upload file {filename}: {e}", exc_info=True)
        bot.send_message(chat_id, f"⚠️ Failed to upload file: `{filename}`.\nError: {e}")
        return False

    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

# --- Handle Mega Links ---

@bot.message_handler(regexp="https://mega\\.nz/")
def handle_mega(message):
    message_text = message.text
    chat_id = message.chat.id
    user_id = message.from_user.id

    status_msg = bot.send_message(chat_id, "🔍 **Processing your Mega.nz link...**")

    match = re.search(r"(https://mega\\.nz/[^\s)\]]+)", message_text)
    if not match:
        edit_message(status_msg, "❌ Invalid Mega.nz link.")
        delete_message(status_msg, delay=10)
        return

    url = match.group(1).strip()
    job_id = str(uuid.uuid4())
    download_dir = os.path.join(DOWNLOAD_DIR, f"user_{user_id}", job_id)
    os.makedirs(download_dir, exist_ok=True)

    try:
        is_folder = "/folder/" in url

        if is_folder:
            logger.info(f"📁 Folder link detected: {url}")
            edit_message(status_msg, "📁 **Folder detected!**\n⬇️ Downloading files...")

            try:
                mega.download(url, path=download_dir)
            except Exception as e:
                raise Exception(f"Folder download failed: {e}")

            files_to_upload = []
            for root, _, files in os.walk(download_dir):
                for file in files:
                    files_to_upload.append((os.path.join(root, file), file))

            if not files_to_upload:
                edit_message(status_msg, "ℹ️ Folder is empty.")
                delete_message(status_msg, delay=10)
                return

            edit_message(status_msg, f"✅ **Download complete!**\nFound `{len(files_to_upload)}` files.\nStarting upload...")

            uploaded = 0
            for i, (file_path, file_name) in enumerate(files_to_upload):
                if upload_file(chat_id, status_msg, file_path, file_name, i+1, len(files_to_upload)):
                    uploaded += 1

            edit_message(status_msg, f"✅ **Upload complete!**\nSent `{uploaded}` file(s).")

        else:
            logger.info(f"📄 File link detected: {url}")
            edit_message(status_msg, "⏳ **Fetching file info...**")

            try:
                filename = mega.filename(url)
            except MegaError as e:
                edit_message(status_msg, f"❌ Invalid link: `{e}`")
                return

            if not filename:
                edit_message(status_msg, "❌ Could not get filename.")
                return

            file_path = os.path.join(download_dir, filename)
            edit_message(status_msg, f"⬇️ **Downloading:** `{filename}`")

            try:
                mega.download(url, path=file_path)
            except Exception as e:
                raise Exception(f"File download failed: {e}")

            upload_file(chat_id, status_msg, file_path, filename, 1, 1)
            edit_message(status_msg, "✅ **Upload complete!**")

        delete_message(status_msg, delay=10)

    except Exception as e:
        logger.error(f"Unexpected error for {url}: {e}", exc_info=True)
        edit_message(status_msg, f"⚠️ **Unexpected error:**\n`{e}`")
        delete_message(status_msg, delay=10)

    finally:
        clean_directory(download_dir)

# --- Commands ---

@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, (
        "╭━◝━━━━━━━━━━━━◜━╮\n"
        "⚡❍⊱❁ **MEGA.NZ BOT** ❁⊰❍⚡\n"
        "╰━◞━━━━━━━━━━━━◟━╯\n\n"
        "👋 **Welcome!** Paste your Mega.nz URL below 👇\n"
        "I’ll download and send the file(s) directly here ⚡"
    ))

@bot.message_handler(commands=['help'])
def help_command(message):
    bot.reply_to(message, (
        "**How to use:**\n"
        "1️⃣ Send me any public `mega.nz` link (file/folder).\n"
        "2️⃣ I’ll download and upload it here.\n\n"
        "**Supports:**\n"
        "✅ Files & Folders\n"
        "✅ Video duration\n"
        "✅ Auto cleanup"
    ))

@bot.message_handler(commands=['ping'])
def ping_command(message):
    start_time = time.time()
    msg = bot.reply_to(message, "Pong...")
    ping_time = round((time.time() - start_time) * 1000, 2)
    edit_message(msg, f"**Pong!** `{ping_time} ms`")

# --- Startup ---

def main():
    try:
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        logger.info("🚀 Mega.nz Telegram Bot is running...")
        if BOT_OWNER_ID:
            logger.info(f"👑 Owner ID: {BOT_OWNER_ID}")
        bot.polling(non_stop=True)
    except Exception as e:
        logger.critical(f"Polling crashed: {e}", exc_info=True)
        time.sleep(10)
        main()

if __name__ == "__main__":
    main()
