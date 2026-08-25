#!/usr/bin/env bash
# Стартует от root: готовит тома, логинит Codex, роняет привилегии до gateway.
set -euo pipefail

DATA_DIR="${DATA_DIR:-/data}"
AGENT_HOME="/home/agent"

echo "==> Готовлю каталоги данных"
install -d -m 2775 -o gateway -g work "$DATA_DIR" "$DATA_DIR/worktrees" "$DATA_DIR/logs"
install -d -m 0700 -o agent -g work "$AGENT_HOME/.codex"

# Полный chown нужен только один раз — на свежем томе. Дальше файлы уже
# создаются нужными пользователями, а рекурсия по репозиторию дорогая.
if [[ ! -f "$DATA_DIR/.initialized" ]]; then
  chown -R gateway:work "$DATA_DIR"
  find "$DATA_DIR" -type d -exec chmod 2775 {} + || true
  touch "$DATA_DIR/.initialized"
  chown gateway:work "$DATA_DIR/.initialized"
fi

# Репозиторий трогают два пользователя (gateway пушит, agent коммитит),
# поэтому проверку владельца в git нужно ослабить осознанно.
git config --system --add safe.directory '*'

run_as_agent() { gosu agent env HOME="$AGENT_HOME" "$@"; }

if run_as_agent codex login status >/dev/null 2>&1; then
  echo "==> Codex уже авторизован"
elif [[ -n "${CODEX_API_KEY:-}" ]]; then
  echo "==> Авторизую Codex по API-ключу"
  printf '%s' "$CODEX_API_KEY" | run_as_agent codex login --with-api-key \
    || echo "!! Не удалось войти по CODEX_API_KEY — проверьте ключ"
else
  cat <<'MSG'
!! Codex не авторизован и CODEX_API_KEY не задан.
   Вариант 1 (API-ключ): добавьте CODEX_API_KEY в переменные окружения и перезапустите.
   Вариант 2 (аккаунт ChatGPT): выполните разово в терминале контейнера
     gosu agent env HOME=/home/agent codex login --device-auth
   Шлюз запустится, но заявки будут падать до авторизации.
MSG
fi

umask 002
echo "==> Запускаю шлюз"
exec gosu gateway "$@"
