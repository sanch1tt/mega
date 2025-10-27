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
    logger.info("Bot initialized with pyTelegramBotAPI")
except Exception as e:
    logger.critical(f"Failed to initialize bot or Megatools: {e}")
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
        size_mb = round(size_bytes / (1024 * 1024), 2)
        ext = os.path.splitext(filename)[1].lower() or ".file"

        caption = (
            f"📂 **File:** `{filename}`\n"
            f"📦 **Size:** {size_mb} MB\n"
            f"📄 **Extension:** `{ext}`"
        )

        edit_message(status_msg, f"📤 **Uploading file {file_index}/{total_files}:**\n`{filename}`")

        is_video = ext in [".mp4", ".mov", ".mkv", ".avi"]
        duration = get_video_duration(file_path) if is_video else 0

        with open(file_path, 'rb') as f:
            if is_video:
                # ✅ Spoiler only for videos
                bot.send_video(
                    chat_id,
                    f,
                    caption=caption,
                    duration=duration,
                    supports_streaming=True,
                    has_spoiler=True
                )
            else:
                # ✅ No spoiler for docs/images
                bot.send_document(
                    chat_id,
                    f,
                    caption=caption
                )

        logger.info(f"Uploaded {filename}.")
        return True

    except Exception as e:
        logger.error(f"Failed to upload file {file_path}: {e}", exc_info=True)
        bot.send_message(chat_id, f"⚠️ Failed to upload file: `{filename}`. Error: {e}")
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

    status_msg = bot.send_message(chat_id, "🔍 **Processing your link...**")

    match = re.search(r"(https://mega\\.nz/[^\s)\]]+)", message_text)
    if not match:
        edit_message(status_msg, "❌ **Error:** Invalid Mega.nz link.")
        delete_message(status_msg, delay=10)
        return

    url = match.group(1).strip()
    job_id = str(uuid.uuid4())
    download_dir = os.path.join(DOWNLOAD_DIR, f"user_{user_id}", job_id)
    os.makedirs(download_dir, exist_ok=True)

    try:
        is_folder = "/folder/" in url

        if is_folder:
            logger.info(f"User {user_id} sent folder link: {url}")
            edit_message(status_msg, "📁 **Folder link detected.**\n⬇️ Downloading...")

            try:
                mega.download(url, path=download_dir)
                logger.info(f"Folder downloaded to: {download_dir}")
            except Exception as e:
                raise Exception(f"Folder download failed: {e}")

            files_to_upload = []
            for root, dirs, files in os.walk(download_dir):
                for file in files:
                    files_to_upload.append((os.path.join(root, file), file))

            if not files_to_upload:
                edit_message(status_msg, "ℹ️ Folder is empty.")
                delete_message(status_msg, delay=10)
                return

            edit_message(status_msg, f"✅ **Download complete!**\nFound {len(files_to_upload)} files.\nStarting upload...")

            uploaded_count = 0
            for i, (file_path, file_name) in enumerate(files_to_upload):
                if upload_file(chat_id, status_msg, file_path, file_name, i+1, len(files_to_upload)):
                    uploaded_count += 1

            edit_message(status_msg, f"✅ **Upload complete!**\nSuccessfully sent {uploaded_count} file(s).")

        else:
            logger.info(f"User {user_id} sent file link: {url}")
            edit_message(status_msg, "⏳ **Fetching file info...**")

            try:
                filename = mega.filename(url)
            except MegaError as e:
                edit_message(status_msg, f"❌ **Error:** Invalid link.\n`{e}`")
                return

            if not filename:
                edit_message(status_msg, "❌ **Error:** Could not fetch filename.")
                return

            file_path = os.path.join(download_dir, filename)
            edit_message(status_msg, f"⬇️ **Downloading:** `{filename}`")

            try:
                mega.download(url, path=file_path)
                logger.info(f"File downloaded to: {file_path}")
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
    user_id = message.from_user.id
    logger.info(f"User {user_id} started the bot.")
    start_caption = (
        "╭━◝━━━━━━━━━━━━◜━╮\n"
        "⚡❍⊱❁ **MEGA.NZ BOT** ❁⊰❍⚡\n"
        "╰━◞━━━━━━━━━━━━◟━╯\n\n"
        "**Welcome!** I can download files and folders from Mega.nz for you.\n\n"
        "📘 **How It Works:**\n"
        "➤ Paste your Mega.nz URL below 👇\n"
        "➤ The bot will fetch & send the file(s) ⚡\n\n"
        "✨❍⭕️━━━━━━━━━━━━━━━⭕️❍✨"
    )
    bot.reply_to(message, start_caption)

@bot.message_handler(commands=['help'])
def help_command(message):
    help_text = (
        "**How to use the Mega.nz Bot:**\n\n"
        "1️⃣ Send me any public `mega.nz` link (file or folder).\n"
        "2️⃣ I will download and upload the content here.\n\n"
        "**Features:**\n"
        "✅ Supports files & folders\n"
        "✅ Shows video duration\n"
        "✅ Cleans up after upload"
    )
    bot.reply_to(message, help_text)

@bot.message_handler(commands=['ping'])
def ping_command(message):
    start_time = time.time()
    msg = bot.reply_to(message, "Pong!")
    end_time = time.time()
    ping_time = round((end_time - start_time) * 1000, 2)
    edit_message(msg, f"**Pong!**\n`{ping_time} ms`")

# --- Startup ---

def main():
    try:
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        logger.info("🚀 Mega.nz Telegram Bot is running...")
        if BOT_OWNER_ID:
            logger.info(f"Bot Owner ID is set: {BOT_OWNER_ID}")
        bot.polling(non_stop=True)
    except Exception as e:
        logger.critical(f"Bot polling failed: {e}", exc_info=True)
        time.sleep(10)
        main()

if __name__ == "__main__":
    main()
