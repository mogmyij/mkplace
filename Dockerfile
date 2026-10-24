# Playwright image ships Chromium and its system deps for amd64 and arm64 (Raspberry Pi 4/5 on a
# 64-bit OS). The tag matches the pinned playwright package.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

# tzdata so TZ=Europe/London etc. applies to the active-hours setting.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

ENV DATA_DIR=/data PYTHONUNBUFFERED=1 RUN_WORKER=1
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"

# One process runs both the web UI and the crawl scheduler. Keep a single uvicorn worker so the
# scheduler isn't started twice.
CMD ["uvicorn", "app.web:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
