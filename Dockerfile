# InstaBaileys — the Baileys for Instagram.
#
# The image is deliberately small and has no browser in it: that is the whole
# point. Chromium would need 400–500 MB on its own, which is the entire
# memory budget of a free Render instance. This one runs in well under 100 MB.

# --- the fast engine ---------------------------------------------------
# The Rust half: bulk listing of the follower/following chains, the diff
# between them, and the actions that go with them. It is optional at
# runtime — the bot falls back to Python when the binary is not there.
FROM rust:1-slim AS engine
WORKDIR /build
COPY rust/igfast ./rust/igfast
RUN cd rust/igfast && cargo build --release && \
    cp target/release/igfast /build/igfast

FROM python:3.11-slim

# glibc spawns a malloc arena per thread, which quietly wastes memory in a
# small container. Two arenas is plenty for a single-process bot.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    MALLOC_ARENA_MAX=2 \
    IGBOT_ROOT=/app

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
# Installs the package itself, which puts the `igbot` command on the PATH.
RUN pip install --no-cache-dir -e . \
    && mkdir -p /app/state /app/logs /app/secrets /app/bin

COPY --from=engine /build/igfast /app/bin/igfast
RUN chmod +x /app/bin/igfast

EXPOSE 8000

HEALTHCHECK --interval=5m --timeout=20s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/api/health',timeout=10)"

CMD ["igbot", "--root", "/app", "watch"]
