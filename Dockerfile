FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    megatools \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Create writable directories for sessions and downloads
RUN mkdir -p /data/downloads && chmod -R 777 /data

# Install all Python dependencies directly
RUN pip install --no-cache-dir \
    telebot \
    requests \
    flask \
    humanize \
    pymegatools

# Copy main bot file
COPY main.py .

# Expose port for Flask webserver (used for keep-alive / HF Spaces)
EXPOSE 7860

# Run the bot
CMD ["python", "main.py"]
