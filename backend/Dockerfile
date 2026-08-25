# syntax=docker/dockerfile:1
FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Build tooling is needed for the scientific wheels on some platforms, then
# dropped so it does not ship in the runtime image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential curl \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && apt-get purge -y build-essential && apt-get autoremove -y

COPY app ./app

# FastF1's cache and the model artifacts are state, not image content: mount
# them as volumes so retrained models survive a redeploy.
ENV F1_FASTF1_CACHE=/data/fastf1_cache \
    F1_MODEL_DIR=/data/model_artifacts \
    F1_DATABASE_URL=sqlite:////data/f1_dashboard.db
VOLUME ["/data"]

RUN useradd --create-home --uid 10001 appuser \
 && mkdir -p /data && chown -R appuser:appuser /data
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://localhost:8000/health || exit 1

# One worker on purpose: the live loops, the job thread and the broadcaster are
# per-process state. Scale by running one process per session group, not by
# adding workers that would each poll OpenF1 independently.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
