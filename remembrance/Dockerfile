FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 REMEMBRANCE_DATA=/data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home remembrance && mkdir /data && chown remembrance:remembrance /data
COPY --chown=remembrance:remembrance app.py schema.sql consent.py database.py succession.py ./
COPY --chown=remembrance:remembrance migrations ./migrations
COPY --chown=remembrance:remembrance templates ./templates
COPY --chown=remembrance:remembrance static ./static
COPY --chown=remembrance:remembrance scripts ./scripts
USER remembrance
EXPOSE 8787
CMD ["gunicorn", "--bind", "0.0.0.0:8787", "--workers", "2", "--threads", "2", "--timeout", "120", "--worker-tmp-dir", "/tmp", "--error-logfile", "-", "app:create_app()"]
