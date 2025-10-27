import os
import time
import telebot
import requests
import humanize
import subprocess
from datetime import timedelta
from requests_toolbelt.multipart.encoder import MultipartEncoder, MultipartEncoderMonitor

# --- Environment variables (safe) ---
API_ID = int(os.environ.get("API_ID", 20687211))
API_HASH = os.environ.get("API_HASH", "4523f58b045175baaeaf1ba29733f31c")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8318388017:AAGfxwJhAUiFB3xMQ5Sid4rgF0nJHsVUqsw")
BOT_OWNER_ID = int(os.environ.get("BOT_OWNER_ID", 7014665654))

# --- Init bot ---
bot = telebot.TeleBot(BOT_TOKEN)
DOWNLOAD_DIR = "/data/downloads"

# --- Helpers ---
def format_eta(seconds):
    return str(timedelta(seconds=int(seconds)))

def readable_size(bytes_):
    return humanize.naturalsize(bytes_, binary=True)

def send_progress(chat_id, prefix, percent, speed, eta, last_msg_id=None):
    bar_len = 20
    filled_len = int(round(bar_len * percent / 100))
    bar = "▓" * filled_len + "░" * (bar_len - filled_len)
    text = f"{prefix}\n{bar} {percent:.1f}%\nSpeed: {speed}/s | ETA: {eta}"
    if last_msg_id:
        try:
            bot.edit_message_text(text, chat_id, last_msg_id)
            return last_msg_id
        except:
            pass
    msg = bot.send_message(chat_id, text)
    return msg.message_id

# --- MEGA Downloader ---
def mega_download(link, user_id):
    folder = f"{DOWNLOAD_DIR}/user_{user_id}_{int(time.time())}"
    os.makedirs(folder, exist_ok=True)

    cmd = ["megatools", "dl", "--path", folder, link]
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out, err = process.communicate()

    if "Download complete" in out or "Download complete" in err:
        files = []
        for root, _, filenames in os.walk(folder):
            for f in filenames:
                path = os.path.join(root, f)
                files.append(path)
        return files
    else:
        raise Exception(f"Download failed: {err or out}")

# --- Telegram Uploader ---
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
        response = requests.post(url, data=monitor, params=params, headers={'Content-Type': monitor.content_type})
        if response.status_code != 200:
            raise Exception(response.text)
    if last_msg:
        bot.delete_message(chat_id, last_msg)
    bot.send_message(chat_id, f"✅ Uploaded successfully!\n📁 {filename}\n📦 {readable_size(filesize)}")

# --- Command Handler ---
@bot.message_handler(commands=["start"])
def start_msg(msg):
    bot.reply_to(msg, "👋 Send me a MEGA link and I’ll download and upload it here.")

# --- MEGA link detector ---
@bot.message_handler(func=lambda m: "mega.nz" in m.text)
def handle_mega(msg):
    chat_id = msg.chat.id
    link = msg.text.strip()
    bot.send_message(chat_id, f"📥 Detected MEGA link.\nStarting download...")

    try:
        files = mega_download(link, chat_id)
        if not files:
            bot.send_message(chat_id, "❌ No files found in MEGA link.")
            return

        for file_path in files:
            if os.path.exists(file_path):
                bot.send_message(chat_id, f"📤 Uploading `{os.path.basename(file_path)}` ...", parse_mode="Markdown")
                upload_file(chat_id, file_path)
                os.remove(file_path)
                print(f"Cleaned up: {file_path}")
            else:
                bot.send_message(chat_id, f"⚠️ File missing: {file_path}")

    except Exception as e:
        bot.send_message(chat_id, f"❌ Error: {str(e)}")

# --- Run Bot ---
if __name__ == "__main__":
    print("🤖 Mega Telegram Bot started...")
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    bot.infinity_polling(skip_pending=True)
