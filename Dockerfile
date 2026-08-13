FROM python:3.13-slim

# ffmpeg para extraer audio de vídeos + node para el solver JS de yt-dlp (challenges de YouTube)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    nodejs \
    npm \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# yt-dlp actualizado
RUN pip install --no-cache-dir -U yt-dlp

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY static/ static/

ENV MUSIC_DIR=/app/music \
    YT_PROXY=socks5://85.208.48.210:1080

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
