# syntax=docker/dockerfile:1
# Образ для production: Python 3.13 + зависимости из requirements.txt + код, миграции и статика.
# Без тестов и инструментов разработки.

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APP_ENV=production \
    HOST=0.0.0.0 \
    PORT=3000

WORKDIR /app

# Сначала только зависимости — этот слой кэшируется и не пересобирается при правке кода.
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY cryptopay ./cryptopay
COPY migrations ./migrations
COPY public ./public

# Не запускаем приложение от root.
RUN useradd --create-home --uid 10001 app
USER app

EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"3000\")}/healthz', timeout=4)" || exit 1

CMD ["python", "-m", "cryptopay"]
