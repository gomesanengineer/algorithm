FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# ffmpeg: merge video+audio / make mp3 / cut clips.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg fonts-dejavu fonts-liberation fonts-noto-core ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

COPY app ./app

RUN useradd -m appuser && chown -R appuser /app /ms-playwright
USER appuser

EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --timeout-keep-alive 600"]
