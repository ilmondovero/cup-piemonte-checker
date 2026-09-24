FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DB_PATH=/data/cup.db TZ=Europe/Rome
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt     && useradd --system --uid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin cupbot     && mkdir /data && chown cupbot:cupbot /data && chmod 700 /data
COPY cup_http.py store.py bot.py ./

# gira come utente senza privilegi; il database cifrato sta nel volume /data
USER cupbot
VOLUME /data
CMD ["python", "bot.py"]
