import os
import time
import telebot
import humanize
import requests
from datetime import timedelta
from pymegatools import MegaDownloader
from requests_toolbelt import MultipartEncoder, MultipartEncoderMonitor

# --- ENV VARS (safe defaults) ---
API_ID = int(os.environ.get("API_ID", 20687211))
API_HASH = os.environ.get("API_HASH", "4523f58b045175baaeaf1ba29733f31c")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8318388017:AAGfxwJhAUiFB3xMQ5Sid4rgF0nJHsVUqsw")
BOT_OWNER_ID = int(os.environ.get("BOT_OWNER_ID", 7014665654))

bot = telebot.TeleBot(BOT_TOKEN)
DOWNLOAD_DIR = "/data/downloads"

# --- Helper Functions ---
def readable_size(size):
    return humanize.naturalsize(size, binary=True)

def format_eta(seconds):
    return str(timedelta(seconds=int(seconds)))

def send_progress(chat_id, prefix, percent, speed, eta, last_msg_id=None):
    bar_len = 20
    filled_len = int(round(bar_len * percent / 100))
    bar = "▓" * filled_len + "░" * (bar_len - filled_len)
    text = f"{prefix}\n{bar} {percent:.1f}%\n⚡ {speed}/s | ⏱️ ETA: {eta}"

    if last_msg_id:
        try:
            bot.edit_message_text(text, chat_id, last_msg_id)
            return last_msg_id
        except:
            pass
    msg = bot.send_message(chat_id, text)
    return msg.message_id

# --- Download from MEGA using pymegatools ---
def mega_download(link, user_id):
    folder = f"{DOWNLOAD_DIR}/user_{user_id}_{int(time.time())}"
    os.makedirs(folder, exist_ok=True)

    downloader = MegaDownloader()
    files = []

    # Progress callback
    def progress_cb(info):
        nonlocal msg_id
        percent = info.get("progress", 0)
        speed = readable_size(info.get("speed", 0))
        eta = format_eta(info.get("eta", 0))
        msg_id = send_progress(user_id, f"📥 Downloading {info.get('name', '')}", percent, speed, eta, msg_id)

    msg_id = None
    result = downloader.download(link, dest_folder=folder, callback=progress_cb)

    for root, _, filenames in os.walk(folder):
        for f in filenames:
            files.append(os.path.join(root, f))

    if not files:
        raise Exception("No files downloaded from MEGA link.")
    return files

# --- Upload to Telegram ---
def upload_file(chat_id, file_path):
    filename = os.path.basename(file_path)
    filesize = os.path.getsize(file_path)
    start_time = time.time()
    last_msg = None

    def callback(monitor):
        nonlocal last_msg
        elapsed = time.time() - start_time
        percent = (monitor.bytes_read / filesize) * 100
        speed = readable_size(monitor.bytes_read / elapsed) if elapsed else "0 B"
        remaining = (filesize - monitor.bytes_read) / (monitor.bytes_read / elapsed) if monitor.bytes_read and elapsed else 0
        eta = format_eta(remaining)
        last_msg = send_progress(chat_id, f"📤 Uploading {filename}", percent, speed, eta, last_msg)

    with open(file_path, "rb") as f:
        encoder = MultipartEncoder({"document": (filename, f)})
        monitor = MultipartEncoderMonitor(encoder, callback)
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
        params = {"chat_id": chat_id, "caption": f"✅ {filename}\n📦 {readable_size(filesize)}"}
        res = requests.post(url, data=monitor, params=params, headers={"Content-Type": monitor.content_type})
        if res.status_code != 200:
            raise Exception(res.text)
    if last_msg:
        bot.delete_message(chat_id, last_msg)
    bot.send_message(chat_id, f"✅ Upload complete!\n📁 {filename}\n📦 {readable_size(filesize)}")

# --- /start Command ---
@bot.message_handler(commands=["start"])
def start_cmd(msg):
    bot.reply_to(msg, "👋 Send any MEGA link and I’ll download + upload it here with progress updates.")

# --- Handle MEGA link ---
@bot.message_handler(func=lambda m: "mega.nz" in m.text)
def handle_mega(msg):
    chat_id = msg.chat.id
    link = msg.text.strip()
    bot.send_message(chat_id, f"🔗 MEGA link detected.\nStarting download...")

    try:
        files = mega_download(link, chat_id)
        for file_path in files:
            if os.path.exists(file_path):
                bot.send_message(chat_id, f"📤 Preparing `{os.path.basename(file_path)}` for upload...", parse_mode="Markdown")
                upload_file(chat_id, file_path)
                os.remove(file_path)
                print(f"✅ Cleaned up: {file_path}")
            else:
                bot.send_message(chat_id, f"⚠️ Missing file: {file_path}")
    except Exception as e:
        bot.send_message(chat_id, f"❌ Error: {str(e)}")

# --- Main ---
if __name__ == "__main__":
    print("🤖 Mega Bot (pymegatools) started...")
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    bot.infinity_polling(skip_pending=True)
