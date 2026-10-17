FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATABASE_PATH=/data/catalog.sqlite3

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN python -m pip install --no-cache-dir . \
    && groupadd --gid 10001 bot \
    && useradd --uid 10001 --gid bot --no-create-home bot \
    && mkdir /data \
    && chown bot:bot /data
COPY examples ./examples
COPY evals/run.py evals/cases.json ./evals/
COPY evals/free_run.py evals/free_cases.json ./evals/

USER bot
VOLUME ["/data"]
CMD ["python", "-m", "shop_assistant", "run"]
