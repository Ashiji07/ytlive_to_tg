FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY yt_live_bot.py .

# data = pyrogram session, downloads = temporary recordings
VOLUME ["/app/data", "/app/downloads"]

# Har start pe yt-dlp update hota hai (YouTube changes ki wajah se zaroori)
CMD ["sh", "-c", "pip install -U --no-cache-dir yt-dlp && python yt_live_bot.py"]
