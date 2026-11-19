# Multi-arch (amd64, arm64, arm/v7): pure Python + websockets, no native build step.
# Built and pushed with `make image-push` (docs/docker-nginx.md, section 6).
FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/aesaganda/aoe2-on-browser" \
      org.opencontainers.image.title="aoe2-recomp-web" \
      org.opencontainers.image.description="Web server and multiplayer relay for the AoE2 recomp browser build (no game data inside)"

ARG AOE2_VERSION=""
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AOE2_NO_WATCH=1 \
    AOE2_VERSION=${AOE2_VERSION}

WORKDIR /app

COPY deploy/requirements.txt /app/deploy/requirements.txt
RUN pip install --no-cache-dir -r /app/deploy/requirements.txt && \
    groupadd --gid 1000 aoe2 && \
    useradd --uid 1000 --gid 1000 --home-dir /app --shell /sbin/nologin aoe2 && \
    mkdir -p /var/cache/aoe2 /app/web /app/build/web /app/install && \
    chown -R aoe2:aoe2 /var/cache/aoe2 /app

COPY --chown=aoe2:aoe2 tools/web/serve.py /app/tools/web/serve.py
COPY --chown=aoe2:aoe2 tools/net/relay.py /app/tools/net/relay.py
COPY --chown=aoe2:aoe2 web/ /app/web/

USER aoe2
STOPSIGNAL SIGTERM
EXPOSE 8642 8643 8644

CMD ["python3", "/app/tools/web/serve.py", "--host", "0.0.0.0", "--port", "8642"]
