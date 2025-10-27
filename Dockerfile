FROM python:3.11-slim

WORKDIR /app

# Install system dependencies (megatools for the bot, ffmpeg for video)
RUN apt-get update && apt-get install -y \
    megatools \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Create writable directories for session and downloads
# This now includes the 'downloads' folder inside /data
RUN mkdir -p /data/downloads && chmod -R 777 /data

# Copy and install Python requirements
# We install directly to avoid 'requirements.txt' errors
RUN pip install --no-cache-dir \
    humanize \
    pymegatools \
    telebot \
    requests \
    flask

# Copy all application files
COPY main.py .
COPY app.py .
COPY start.sh .

# Make the start script executable
RUN chmod +x start.sh

# Expose the web server port (default 7860 for HF Spaces)
EXPOSE 7860

# Run the start script
CMD ["./start.sh"]

