FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libpq5 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app ./app
COPY locales ./locales
COPY alembic ./alembic
COPY alembic.ini .
COPY pyproject.toml .
COPY tests ./tests
# uid pinned: backup.sh chowns the dump directory to it so `web` can read a
# 0700 directory it does not own (IMPLEMENTATION.md §16.6).
RUN adduser --disabled-password --gecos "" --uid 1000 appuser && chown -R appuser /app
USER appuser
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
