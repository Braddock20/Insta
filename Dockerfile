FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    IGBOT_HEADLESS=true \
    IGBOT_PROFILE_DIR=/app/state/profile \
    IGBOT_LOG_FILE=/app/state/igbot.log

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium

COPY . .

# The state directory holds the SQLite database, the browser profile and the
# log. On a paid service it is a persistent disk; on a free service it is
# ephemeral, and the bot re-primes itself on every cold start.
RUN mkdir -p /app/state /app/logs /app/secrets \
    && chmod 700 /app/secrets

EXPOSE 8000

# The bot runs the watch loop and, when api.enabled is on, the HTTP control
# API in the same process. Render's free web services sleep after 15 minutes
# without traffic, so /api/health doubles as the keep-alive endpoint.
CMD ["python", "-m", "igbot", "watch"]
