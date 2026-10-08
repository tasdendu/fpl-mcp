# syntax=docker/dockerfile:1.7
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --create-home app

WORKDIR /app

COPY requirements.lock ./
RUN pip install -r requirements.lock

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-deps .

# Writable home for the persisted refresh token (mounted as a named volume).
RUN mkdir -p /data && chown app:app /data

USER app
EXPOSE 8000

CMD ["fpl-mcp"]
