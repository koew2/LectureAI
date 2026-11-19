FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv/eduflow
RUN groupadd --gid 10001 eduflow && useradd --uid 10001 --gid eduflow --create-home eduflow
COPY pyproject.toml README.md ./
COPY app ./app
RUN python -m pip install .
COPY alembic.ini ./
COPY alembic ./alembic
RUN mkdir -p /srv/eduflow/uploads && chown eduflow:eduflow /srv/eduflow/uploads

FROM base AS api
USER eduflow
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS worker
USER root
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install '.[worker]'
ENV HF_HOME=/home/eduflow/.cache/huggingface
RUN mkdir -p /home/eduflow/.cache/huggingface \
    && chown -R eduflow:eduflow /home/eduflow/.cache
USER eduflow
CMD ["celery", "-A", "app.worker:celery_app", "worker", "--loglevel=INFO", "--concurrency=1"]
