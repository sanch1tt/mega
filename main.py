import os
import re
import logging
from telebot import TeleBot
from mega import Mega

# ------------------- CONFIG -------------------
API_ID = int(os.environ.get("API_ID", 20687211))
API_HASH = os.environ.get("API_HASH", "4523f58b045175baaeaf1ba29733f31c")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8318388017:AAGfxwJhAUiFB3xMQ5Sid4rgF0nJHsVUqsw")
BOT_OWNER_ID = int(os.environ.get("BOT_OWNER_ID", 7014665654))

# ------------------- SETUP -------------------
bot = TeleBot(BOT_TOKEN)
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# ------------------- HELPER FUNCTIONS -------------------

def is_mega_link(url: str):
    """Return True if link is a valid Mega.nz file or folder link."""
    pattern = r'https://mega\.nz/(file|folder)/[A-Za-z0-9_-]+#[A-Za-z0-9_-]+'
    return bool(re.match(pattern, url))


def download_from_mega(url: str):
    """Download file/folder from Mega.nz and return local file path(s)."""
    try:
        mega = Mega()
        m = mega.login()  # guest login

        if "/folder/" in url:
            folder = m.download_url(url)
            paths = []
            for root, _, files in os.walk(folder):
                for file in files:
                    paths.append(os.path.join(root, file))
            return paths
        else:
            path = m.download_url(url)
            return [path]
    except Exception as e:
        logger.error(f"❌ Mega download failed: {e}", exc_info=True)
        return None


def edit_message(msg, text):
    """Safely edits Telegram messages."""
    try:
        bot.edit_message_text(text, msg.chat.id, msg.message_id, parse_mode="Markdown")
    except Exception as e:
        logger.warning(f"Edit message failed: {e}")


def upload_file(chat_id, status_msg, file_path, filename, file_index, total_files):
    """Detect file type and upload to Telegram."""
    try:
        size_bytes = os.path.getsize(file_path)
        size_mb = round(size_bytes / (1024 * 1024), 2)
        ext = os.path.splitext(filename)[1].lower()

        caption = (
            f"📂 **File:** `{filename}`\n"
            f"📦 **Size:** {size_mb} MB\n"
            f"📄 **Extension:** `{ext}`"
        )

        edit_message(status_msg, f"📤 Uploading file {file_index}/{total_files}: `{filename}`")

        with open(file_path, 'rb') as f:
            if ext in [".mp4", ".mkv", ".mov", ".avi", ".webm"]:
                bot.send_video(chat_id, f, caption=caption, supports_streaming=True)
            elif ext in [".mp3", ".wav", ".m4a", ".ogg", ".flac"]:
                bot.send_audio(chat_id, f, caption=caption)
            elif ext in [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"]:
                bot.send_photo(chat_id, f, caption=caption)
            elif ext in [".pdf", ".zip", ".rar", ".7z", ".tar", ".gz", ".txt", ".docx", ".pptx"]:
                bot.send_document(chat_id, f, caption=caption)
            else:
                bot.send_document(chat_id, f, caption=caption)

        logger.info(f"✅ Uploaded {filename} successfully.")
        return True

    except Exception as e:
        logger.error(f"❌ Upload failed for {filename}: {e}", exc_info=True)
        bot.send_message(chat_id, f"⚠️ Failed to upload `{filename}`.\nError: {e}")
        return False

    finally:
        if os.path.exists(file_path):
            os.remove(file_path)


# ------------------- COMMAND HANDLERS -------------------

@bot.message_handler(commands=["start"])
def start_command(message):
    bot.reply_to(message, "👋 Send me any Mega.nz file or folder link to download and upload here.")


@bot.message_handler(func=lambda message: True, content_types=["text"])
def handle_mega(message):
    url = message.text.strip()
    if not is_mega_link(url):
        bot.reply_to(message, "❌ Invalid Mega.nz link.\nPlease send a valid `https://mega.nz/file/...` or `https://mega.nz/folder/...` link.")
        return

    status_msg = bot.send_message(message.chat.id, "🔗 Valid Mega link detected!\n📥 Downloading from Mega.nz...")

    files = download_from_mega(url)
    if not files:
        edit_message(status_msg, "❌ Failed to download from Mega.nz. Please check the link.")
        return

    total = len(files)
    for i, file_path in enumerate(files, start=1):
        filename = os.path.basename(file_path)
        upload_file(message.chat.id, status_msg, file_path, filename, i, total)

    edit_message(status_msg, "✅ All files uploaded successfully!")


# ------------------- RUN -------------------
if __name__ == "__main__":
    logger.info("🚀 Bot started successfully!")
    bot.infinity_polling()
