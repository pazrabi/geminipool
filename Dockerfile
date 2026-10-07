FROM node:22-bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Version verified locally with `gemini --help` (flags -p, -o json, --approval-mode, --skip-trust).
RUN npm install -g @google/gemini-cli@0.63.0 && npm cache clean --force

ENV GEMINI_BIN=gemini \
    POOL_DATA=/data \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY app/ /app/

CMD ["python3", "/app/main.py"]
