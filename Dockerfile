FROM python:3.12-slim AS base

WORKDIR /app

RUN adduser --disabled-password --gecos "" botuser

COPY pyproject.toml README.md LICENSE ./
COPY src/ src/

RUN pip install --no-cache-dir .

RUN mkdir -p /app/data && chown botuser:botuser /app/data

USER botuser

VOLUME /app/data

CMD ["python", "-m", "proton_telegram_bot"]
