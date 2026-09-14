FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# A fresh installation gets the tracked source list once. Runtime edits made
# through Telegram are stored in /app/data and survive image upgrades.
COPY config/sources.yaml /app/defaults/sources.yaml
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint
RUN chmod 755 /usr/local/bin/docker-entrypoint \
    && mkdir -p /app/data

ENTRYPOINT ["docker-entrypoint"]
CMD ["python", "-m", "news_monitor"]
