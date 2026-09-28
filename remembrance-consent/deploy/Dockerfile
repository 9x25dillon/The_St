# Remembrance consent kernel. Build from remembrance-consent/:
#   docker build -f deploy/Dockerfile -t remembrance-consent .
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv/remembrance-consent

COPY pyproject.toml ./
COPY app ./app
RUN pip install ".[s3]" && useradd --system --uid 10001 --home /srv/remembrance-consent consent

COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts ./scripts

USER consent
EXPOSE 8790
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8790/healthz', timeout=2)"
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8790", "--proxy-headers", "--forwarded-allow-ips", "*"]
