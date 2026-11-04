# The API runs as a long-lived container, not a function: it holds WebSocket
# connections open, runs training on background threads, and keeps a disk cache
# of model artifacts in front of the bucket. None of that survives a serverless
# platform, which is why the dashboard and the API deploy to different places.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# libgomp is XGBoost's OpenMP runtime; the slim image does not carry it.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 curl \
 && rm -rf /var/lib/apt/lists/*

# Requirements first, so a code change does not reinstall the ML stack.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Caches, not records: the bucket holds the artifacts and Postgres holds the
# rows. Mount a disk here to keep the cache warm across deploys.
ENV F1_MODEL_DIR=/var/cache/f1/model_artifacts \
    F1_REPLAY_DIR=/var/cache/f1/replays \
    F1_FASTF1_CACHE=/var/cache/f1/fastf1 \
    F1_ENV=production \
    PORT=8000
RUN mkdir -p /var/cache/f1

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s \
  CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

# One worker on purpose: the live loops, the job queue and the broadcaster are
# process-local, so a second worker would run a second copy of each.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1"]
