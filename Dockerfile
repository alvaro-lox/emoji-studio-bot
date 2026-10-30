FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    IMAGEIO_FFMPEG_EXE=/usr/bin/ffmpeg \
    DATA_DIR=/data \
    U2NET_HOME=/data/models

# ffmpeg кодирует видео, Inter — шрифт подписей как в Telegram (DejaVu — запасной)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-inter fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /bot
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
RUN useradd --system --uid 1000 bot && mkdir -p /data && chown bot /data
USER bot
VOLUME /data

CMD ["python", "-m", "app"]
