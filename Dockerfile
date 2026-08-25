# --- Сборка интерфейса (React 19 + HeroUI v3 + Tailwind v4) ------------------
FROM node:22-bookworm-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
# vite складывает результат в ../static → /static
RUN npm run build

# --- Образ шлюза ------------------------------------------------------------
# Внутри два непривилегированных пользователя:
#   gateway — веб-приложение, держит GitHub-токен и токены доступа;
#   agent   — процессы Codex, работают с кодом проекта и токенов не видят.
FROM node:22-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      git curl ca-certificates python3 python3-venv sudo gosu tini \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @openai/codex && npm cache clean --force

RUN groupadd -g 10003 work \
 && useradd -m -u 10001 -g work -s /bin/bash gateway \
 && useradd -m -u 10002 -g work -s /bin/bash agent \
 && echo 'gateway ALL=(agent) NOPASSWD: /usr/local/bin/run-agent.sh' > /etc/sudoers.d/gateway \
 && chmod 0440 /etc/sudoers.d/gateway

WORKDIR /srv
COPY requirements.txt ./
RUN python3 -m venv /srv/venv \
 && /srv/venv/bin/pip install --no-cache-dir --upgrade pip \
 && /srv/venv/bin/pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY --from=web /static ./static
COPY AGENTS.md.template ./
COPY docker/run-agent.sh /usr/local/bin/run-agent.sh
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/run-agent.sh /usr/local/bin/entrypoint.sh

ENV DATA_DIR=/data \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8787
ENTRYPOINT ["tini", "--", "/usr/local/bin/entrypoint.sh"]
# --no-access-log: в ссылках доступа есть токен, ему не место в логах контейнера.
CMD ["/srv/venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8787", "--no-access-log"]
