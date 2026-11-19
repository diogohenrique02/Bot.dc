FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libopus0 libsodium23 \
    && rm -rf /var/lib/apt/lists/*
# Deno: o yt-dlp precisa de um runtime JavaScript para resolver os desafios do YouTube
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .
CMD ["python", "-u", "bot.py"]
