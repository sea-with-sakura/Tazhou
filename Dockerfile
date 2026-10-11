FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BAOTAER_DATA=/app/data
WORKDIR /app
RUN groupadd --gid 10001 baotaer && useradd --uid 10001 --gid baotaer --no-create-home baotaer
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=baotaer:baotaer app ./app
COPY --chown=baotaer:baotaer scripts ./scripts
RUN mkdir -p /app/data /app/backups && chown -R baotaer:baotaer /app/data /app/backups
USER baotaer
EXPOSE 12500
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:12500/api/health', timeout=3)"
CMD ["sh", "-c", "python scripts/bootstrap.py --bind 0.0.0.0 && exec python -m app.server"]
