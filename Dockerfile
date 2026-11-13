# Three stages. The LAST one is the runtime image, which is what `docker compose
# build` and a plain `docker build .` produce: the application, its runtime
# requirements and nothing else -- no compiler, no pytest/mypy/ruff, no tests/.
#
#   base     runtime requirements + the application
#   test     base + requirements-dev.txt + tests/   (CI and the test loop build it:
#            docker build --target test .)
#   runtime  base, unchanged -- the default target, deliberately last
#
# Every pinned requirement ships a manylinux wheel for CPython 3.12, so nothing
# is compiled and build-essential is not needed here (it used to be installed
# and left in the image).
FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app ./app
COPY locales ./locales
COPY alembic ./alembic
COPY alembic.ini .
COPY pyproject.toml .
# uid pinned: backup.sh chowns the dump directory to it so `web` can read a
# 0700 directory it does not own (IMPLEMENTATION.md §16.6).
RUN adduser --disabled-password --gecos "" --uid 1000 appuser && chown -R appuser /app
USER appuser
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS test
USER root
COPY requirements-dev.txt .
RUN pip install -r requirements-dev.txt
COPY tests ./tests
# The start-up decision of the backup sidecar is tested as a shell function.
COPY backup.sh ./backup.sh
RUN chown -R appuser /app
USER appuser

FROM base AS runtime
