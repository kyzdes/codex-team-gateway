# codex-team-gateway — образ для деплоя через Dokploy (или любой Docker/Compose хостинг)
FROM node:20-bookworm-slim

# Codex CLI пишет и читает файлы проекта от имени обычного пользователя,
# а не root — так безопаснее, если Codex сам исполняет shell-команды.
RUN apt-get update && apt-get install -y --no-install-recommends \
      git curl python3 python3-venv python3-pip openssl gosu tini \
      build-essential ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Codex CLI (глобально, доступен и root, и обычному пользователю)
RUN npm install -g @openai/codex

RUN useradd -m -u 1000 -s /bin/bash codex

WORKDIR /app
COPY requirements.txt ./
RUN python3 -m venv /app/venv \
    && /app/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /app/venv/bin/pip install --no-cache-dir -r requirements.txt

COPY app.py ./
COPY static ./static
COPY AGENTS.md.template deploy.sh.template gateway_config.example.json ./
COPY docker-setup-project.sh ./
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh /app/docker-setup-project.sh

# Директории, которые должны жить в volume (см. docker-compose.yml):
#   /home/codex/.codex        — логин/сессии Codex CLI (codex login сохраняется тут)
#   /home/codex/projects       — клонированный репозиторий + worktree на коллег
#   /config                    — gateway config.json с токенами
#   /app/state, /app/logs      — история переписки и аудиторские логи
RUN mkdir -p /home/codex/.codex /home/codex/projects /config /app/state /app/logs \
    && chown -R codex:codex /home/codex /app/state /app/logs /config

ENV CODEX_GATEWAY_CONFIG=/config/config.json \
    CODEX_GATEWAY_STATE_DIR=/app/state \
    CODEX_GATEWAY_LOG_DIR=/app/logs \
    CODEX_BIN=codex \
    HOME=/home/codex

EXPOSE 8787

ENTRYPOINT ["tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
CMD ["/app/venv/bin/uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8787"]
