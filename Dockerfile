FROM python:3.12-slim AS base

WORKDIR /app

# System packages:
#   - gnupg: pre-existing dependency for Proton-bridge auth helpers.
#   - The remaining packages are the runtime libraries Playwright's bundled
#     Chromium needs. We install them now so /genaddr can launch a headless
#     browser inside the container without `playwright install --with-deps`
#     hitting apt at runtime as a non-root user.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gnupg \
        ca-certificates \
        fonts-liberation \
        libasound2 \
        libatk-bridge2.0-0 \
        libatk1.0-0 \
        libatspi2.0-0 \
        libcups2 \
        libdbus-1-3 \
        libdrm2 \
        libgbm1 \
        libglib2.0-0 \
        libgtk-3-0 \
        libnspr4 \
        libnss3 \
        libpango-1.0-0 \
        libwayland-client0 \
        libx11-6 \
        libxcb1 \
        libxcomposite1 \
        libxdamage1 \
        libxext6 \
        libxfixes3 \
        libxkbcommon0 \
        libxrandr2 \
        wget \
        xdg-utils \
    && rm -rf /var/lib/apt/lists/*

RUN adduser --disabled-password --gecos "" botuser

COPY pyproject.toml README.md LICENSE ./
COPY src/ src/

RUN pip install --no-cache-dir .

# Pre-download the Chromium Playwright runs against. Cached in the image so
# the bot starts instantly; placed under /home/botuser so the non-root user
# can read it.
ENV PLAYWRIGHT_BROWSERS_PATH=/home/botuser/.cache/ms-playwright
RUN python -m playwright install chromium \
    && chown -R botuser:botuser /home/botuser/.cache

RUN mkdir -p /app/data && chown botuser:botuser /app/data

USER botuser

VOLUME /app/data

CMD ["python", "-m", "proton_telegram_bot"]
