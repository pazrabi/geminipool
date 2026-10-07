FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ca-certificates openssh-client \
 && rm -rf /var/lib/apt/lists/*

# Antigravity CLI, installed with Google's official script (https://antigravity.google/download).
RUN curl -fsSL https://antigravity.google/cli/install.sh | bash \
 && install -m 0755 "$(find /root -name agy -type f | head -1)" /usr/local/bin/agy \
 && agy --version \
 && (agy --help || true)

ENV AGY_BIN=/usr/local/bin/agy \
    POOL_DATA=/data \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY app/ /app/

CMD ["python3", "/app/main.py"]
