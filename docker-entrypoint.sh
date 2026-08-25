#!/usr/bin/env bash
# Entrypoint контейнера codex-team-gateway.
# Выполняется как root, готовит окружение, дальше передаёт управление
# указанной команде уже от имени пользователя codex (через gosu).
set -euo pipefail

CONFIG_PATH="${CODEX_GATEWAY_CONFIG:-/config/config.json}"

echo "==> Проверяю права на volume..."
chown -R codex:codex /home/codex /app/state /app/logs "$(dirname "${CONFIG_PATH}")" 2>/dev/null || true

if [[ ! -f "${CONFIG_PATH}" ]]; then
  echo "==> Конфиг не найден, генерирую ${CONFIG_PATH} с новыми токенами доступа"
  TOKEN1=$(openssl rand -hex 24)
  TOKEN2=$(openssl rand -hex 24)
  cat > "${CONFIG_PATH}" <<EOF
{
  "sandbox_mode": "danger-full-access",
  "approval_policy": "never",
  "model": null,
  "users": {
    "colleague1": {
      "token": "${TOKEN1}",
      "workdir": "/home/codex/projects/project-colleague1",
      "display_name": "Коллега 1"
    },
    "colleague2": {
      "token": "${TOKEN2}",
      "workdir": "/home/codex/projects/project-colleague2",
      "display_name": "Коллега 2"
    }
  }
}
EOF
  chown codex:codex "${CONFIG_PATH}"
  echo "----------------------------------------------------------------"
  echo " Сгенерированы токены доступа (сохраните — больше не выведутся):"
  echo "   colleague1: ${TOKEN1}"
  echo "   colleague2: ${TOKEN2}"
  echo " Отредактируйте ${CONFIG_PATH} внутри volume, если нужны другие"
  echo " имена/пути, затем перезапустите контейнер."
  echo "----------------------------------------------------------------"
fi

# Codex CLI не умеет интерактивный OAuth-логин внутри контейнера без браузера.
# Поэтому здесь поддерживается вход по API-ключу через переменную окружения
# CODEX_API_KEY (задаётся в Dokploy → Environment). Если она задана и Codex
# ещё не залогинен — логинимся один раз при старте.
if [[ -n "${CODEX_API_KEY:-}" ]]; then
  if ! gosu codex codex login status >/dev/null 2>&1; then
    echo "==> Логиню Codex CLI по API-ключу (CODEX_API_KEY)"
    echo "${CODEX_API_KEY}" | gosu codex codex login --with-api-key || \
      echo "!! Не удалось залогиниться по API-ключу, проверьте CODEX_API_KEY"
  fi
else
  if ! gosu codex codex login status >/dev/null 2>&1; then
    echo "!! Codex CLI ещё не залогинен и CODEX_API_KEY не задан."
    echo "   Вариант 1: задайте CODEX_API_KEY в переменных окружения Dokploy и перезапустите."
    echo "   Вариант 2: выполните разовый логин внутри контейнера:"
    echo "     docker exec -it <container> gosu codex codex login --device-auth"
    echo "   Шлюз всё равно запустится, но запросы к Codex будут падать до логина."
  fi
fi

echo "==> Запускаю: $*"
exec gosu codex "$@"
