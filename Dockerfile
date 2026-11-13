# TeleRocket container. Secrets are passed at runtime (docker compose env_file),
# never copied into the image (see .dockerignore).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN pip install --no-cache-dir "uv==0.12.21"

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY telerocket ./telerocket
RUN uv sync --frozen --no-dev --no-cache && pip uninstall -y uv

RUN useradd --system --uid 10001 --home-dir /app telerocket \
    && mkdir /data && chown telerocket /data && chmod 700 /data
USER telerocket
ENV DATA_DIR=/data
VOLUME ["/data"]

ENTRYPOINT ["/opt/venv/bin/telerocket"]
# Config comes from environment variables, not from a file inside the image.
CMD ["run", "--env-file", "/dev/null/none"]
