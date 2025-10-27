import os
import humanize
import logging
import re      # For regular expressions
import shutil  # For deleting folders
import uuid    # For unique folder names
import time    # For ping
import subprocess # For running ffprobe
import json    # For parsing ffprobe output
from datetime import datetime
import telebot # The new library
from telebot.types import InputFile # For sending files
from pymegatools import Megatools
from pymegatools.pymegatools import MegaError

# 🔹 Configuration
API_ID = int(os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
BOT_OWNER_ID = int(os.environ.get("BOT_OWNER_ID")

# --- File Management ---
# We use a /data directory which is created in the Dockerfile for persistent storage
DOWNLOAD_DIR = "/data/downloads" # This must point to the writable /data directory

# 🔹 Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# 🔹 Initialize
try:
    bot = telebot.TeleBot(BOT_TOKEN, parse_mode='Markdown')
    mega = Megatools()
    logger.info("Bot initialized with pyTelegramBotAPI")
except Exception as e:
    logger.critical(f"Failed to initialize bot or Megatools: {e}")
    exit(1)

# --- Helper Functions ---

def progress_bar(percentage, bar_length=20):
    """Generates a text-based progress bar."""
    filled = int(bar_length * percentage / 100)
    empty = bar_length - filled
    return "█" * filled + "░" * empty

def edit_message(message, text):
    """Safely edits a message."""
    try:
        bot.edit_message_text(text, message.chat.id, message.message_id)
    except Exception as e:
        if "message not modified" not in str(e).lower():
            logger.error(f"Error editing message: {str(e)}")

def delete_message(message, delay=5):
    """Safely deletes a message after a delay."""
    try:
        if delay > 0:
            time.sleep(delay)
        bot.delete_message(message.chat.id, message.message_id)
    except Exception as e:
        logger.error(f"Error deleting message: {str(e)}")

def clean_directory(directory):
    """Safely and recursively removes a directory."""
    try:
        if os.path.exists(directory):
            shutil.rmtree(directory)
            logger.info(f"🪥 Successfully cleaned directory: {directory}")
            return True
        else:
            logger.warning(f"⚠️ Directory does not exist, cannot clean: {directory}")
            return False
    except Exception as e:
        logger.error(f"❌ Error cleaning directory {directory}: {repr(e)}")
        return False

def get_video_duration(file_path):
    """Tries to get video duration using ffprobe (synchronous)."""
    try:
        command = [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", file_path
        ]
        # Use subprocess.run for a synchronous call
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
            logger.warning(f"ffprobe failed (duration): {result.stderr}")
            return 0
    except FileNotFoundError:
        logger.warning("ffprobe not found. Cannot get video duration. (Did you install ffmpeg?)")
        return 0
    except Exception as e:
        logger.error(f"Error in get_video_duration: {e}")
        return 0

# --- End of Helper Functions ---


def upload_file(chat_id, status_msg, file_path, filename, file_index, total_files):
    """
    Handles the upload process for a single file (synchronous).
    """
    try:
        size_bytes = os.path.getsize(file_path)
        size_mb = round(size_bytes / (1024 * 1024), 2)
        ext = os.path.splitext(filename)[1].lower() or ".file"

        caption = (
            f"📂 **File:** `{filename}`\n"
            f"📦 **Size:** {size_mb} MB\n"
            f"📄 **Extension:** `{ext}`"
        )

        edit_message(status_msg, f"📤 **Uploading file {file_index}/{total_files}:**\n`{filename}`\n(Upload progress not available with telebot)")

        # Handle file types
        is_video = ext in [".mp4", ".mov", ".mkv", ".avi"]

        duration = 0
        if is_video:
            duration = get_video_duration(file_path)

        # Open the file and send it
        with open(file_path, 'rb') as f:
            if is_video:
                bot.send_video(
                    chat_id,
                    f,
                    caption=caption,
                    duration=duration,
                    supports_streaming=True,
                    has_spoiler=True
                )
            else:
                bot.send_document(
                    chat_id,
                    f,
                    caption=caption,
                    has_spoiler=True
                )

        logger.info(f"Uploaded {filename}.")
        return True

    except Exception as e:
        logger.error(f"Failed to upload file {file_path}: {e}", exc_info=True)
        bot.send_message(chat_id, f"⚠️ Failed to upload file: `{filename}`. Error: {e}")
        return False

    finally:
        # Clean up individual file after upload
        if os.path.exists(file_path):
            os.remove(file_path)

@bot.message_handler(regexp="https://mega\.nz/")
def handle_mega(message):
    message_text = message.text
    chat_id = message.chat.id
    user_id = message.from_user.id

    status_msg = bot.send_message(chat_id, "🔍 **Processing your link...**")

    match = re.search(r"(https://mega\.nz/[^\s)\]]+)", message_text)

    if not match:
        edit_message(status_msg, "❌ **Error:** Could not find a valid mega.nz link.")
        delete_message(status_msg, delay=10)
        return

    url = match.group(1).strip()

    # Sandboxed directory for this specific job
    job_id = str(uuid.uuid4())
    download_dir = os.path.join(DOWNLOAD_DIR, f"user_{user_id}", job_id)
    os.makedirs(download_dir, exist_ok=True)

    try:
        is_folder = "/folder/" in url

        if is_folder:
            # --- FOLDER LOGIC ---
            logger.info(f"User {user_id} sent folder link: {url}")
            edit_message(status_msg, "📁 **Folder link detected.**\n⬇️ Downloading all files... (This may take a while)")

            try:
                # This is a blocking call.
                mega.download(url, path=download_dir)
                logger.info(f"Folder downloaded to: {download_dir}")
            except Exception as e:
                logger.error(f"Folder download failed for {url}: {e}", exc_info=True)
                raise Exception(f"Folder download failed: {e}")

            edit_message(status_msg, "✅ **Download complete!**\nPreparing to upload...")

            files_to_upload = []
            for root, dirs, files in os.walk(download_dir):
                for file in files:
                    files_to_upload.append((os.path.join(root, file), file))

            if not files_to_upload:
                edit_message(status_msg, "ℹ️ The folder appears to be empty.")
                delete_message(status_msg, delay=10)
                return

            edit_message(status_msg, f"Found {len(files_to_upload)} files. Starting upload...")

            uploaded_count = 0
            for i, (file_path, file_name) in enumerate(files_to_upload):
                if upload_file(chat_id, status_msg, file_path, file_name, i+1, len(files_to_upload)):
                    uploaded_count += 1

            edit_message(status_msg, f"✅ **Upload complete!**\nSuccessfully sent {uploaded_count} file(s).")

        else:
            # --- SINGLE FILE LOGIC ---
            logger.info(f"User {user_id} sent file link: {url}")
            edit_message(status_msg, "⏳ **Fetching file info...**")

            try:
                filename = mega.filename(url)
            except MegaError as e:
                logger.error(f"MegaError (filename): {e}", exc_info=True)
                edit_message(status_msg, f"❌ **Error:** Link seems invalid or expired.\n`{e}`")
                return

            if not filename:
                edit_message(status_msg, "❌ **Error:** Could not fetch filename.")
                return

            file_path = os.path.join(download_dir, filename)
            edit_message(status_msg, f"⬇️ **Downloading:**\n`{filename}`")

            try:
                mega.download(url, path=file_path)
                logger.info(f"File downloaded to: {file_path}")
            except Exception as e:
                logger.error(f"File download failed for {url}: {e}", exc_info=True)
                raise Exception(f"File download failed: {e}")

            upload_file(chat_id, status_msg, file_path, filename, 1, 1)
            edit_message(status_msg, "✅ **Upload complete!**")

        delete_message(status_msg, delay=10)

    except Exception as e:
        logger.error(f"An unexpected error occurred processing {url}: {e}", exc_info=True)
        edit_message(status_msg, f"⚠️ **An unexpected error occurred:**\n`{e}`")
        delete_message(status_msg, delay=10)

    finally:
        clean_directory(download_dir)

# --- Standard Bot Commands ---

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
        "📦 **Limit:** `None (depends on server)`\n"
        "✨❍⭕️━━━━━━━━━━━━━━━⭕️❍✨"
    )
    bot.reply_to(message, start_caption)

@bot.message_handler(commands=['help'])
def help_command(message):
    logger.info(f"User {message.from_user.id} requested help.")
    help_text = (
        "**Welcome to the Mega.nz Bot!**\n\n"
        "**How to use:**\n"
        "1.  Send me any public `mega.nz` file link.\n"
        "2.  Send me any public `mega.nz` folder link.\n"
        "3.  I will download the file(s) and upload them directly to this chat.\n\n"
        "**Features:**\n"
        "✅  Supports both files and folders.\n"
        "✅  Gets video duration (if `ffprobe` is installed).\n"
        "✅  Cleans up files after upload to save space."
    )
    bot.reply_to(message, help_text)

@bot.message_handler(commands=['ping'])
def ping_command(message):
    logger.info(f"User {message.from_user.id} used /ping.")
    start_time = time.time()
    msg = bot.reply_to(message, "Pong!")
    end_time = time.time()
    ping_time = round((end_time - start_time) * 1000, 2)
    edit_message(msg, f"**Pong!**\n`{ping_time} ms`")

# --- Bot Startup ---

def main():
    try:
        # Create required directories on startup
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)

        logger.info("🚀 Mega.nz Telegram Bot is running...")
        if BOT_OWNER_ID != 0:
            logger.info(f"Bot Owner ID is set: {BOT_OWNER_ID}")
        else:
            logger.warning("BOT_OWNER_ID is not set.")

        # Start polling
        bot.polling(non_stop=True)

    except Exception as e:
        logger.critical(f"Bot polling failed: {e}", exc_info=True)
        time.sleep(10) # Wait before retrying
        main()

if __name__ == "__main__":
    main()


