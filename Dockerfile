FROM python:3.11-slim

WORKDIR /app

# Install system dependencies (megatools for MEGA download, ffmpeg for media handling)
RUN apt-get update && apt-get install -y \
    megatools \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Create writable directories for sessions and downloads
RUN mkdir -p /data/downloads && chmod -R 777 /data

# Install all Python dependencies directly (no requirements.txt)
RUN pip install --no-cache-dir \
    telebot \
    requests \
    flask \
    humanize \
    pymegatools \
    requests-toolbelt

# Copy main bot file
COPY main.py .

# Expose Flask web server port (used for uptime pings or Hugging Face Spaces)
EXPOSE 7860

# Run the Telegram bot
CMD ["python", "main.py"]
