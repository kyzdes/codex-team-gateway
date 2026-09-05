#!/usr/bin/env bash
# Стартует от root: готовит тома, логинит Codex, роняет привилегии до gateway.
set -euo pipefail

DATA_DIR="${DATA_DIR:-/data}"
AGENT_HOME="/home/agent"

echo "==> Готовлю каталоги данных"
# 0755, а не 2775: агент состоит в одной группе со шлюзом, поэтому групповая
# запись на самих каталогах данных означает «агент может снести или подменить
# app.db и подсунуть свой каталог рабочей копии». Внутрь рабочей копии он
# пишет и без этого — её файлы шлюз создаёт с umask 002.
install -d -m 0755 -o gateway -g work "$DATA_DIR" "$DATA_DIR/worktrees" "$DATA_DIR/uploads"
# Логи прогонов — переписка по чужим заявкам, поэтому 0700, а не 0750:
# групповое r-x здесь означало бы «агент читает все чужие заявки и правит
# админский аудит». Само приложение подтверждает этот режим на каждом старте.
# База закрывается отдельно, из самого приложения (db._restrict_permissions).
install -d -m 0700 -o gateway -g work "$DATA_DIR/logs"
install -d -m 0700 -o agent -g work "$AGENT_HOME/.codex"

# Полный chown нужен только один раз — на свежем томе. Дальше файлы уже
# создаются нужными пользователями, а рекурсия по репозиторию дорогая.
# Права внутри копии проекта расставляет сам шлюз (gitops._lock_repo): часть
# каталогов .git обязана остаться групповой, иначе агент не закоммитит, а
# конфиг и крючки — наоборот, закрытыми, иначе агент одолжит личность шлюза.
if [[ ! -f "$DATA_DIR/.initialized" ]]; then
  chown -R gateway:work "$DATA_DIR"
  touch "$DATA_DIR/.initialized"
  chown gateway:work "$DATA_DIR/.initialized"
fi

# Репозиторий трогают два пользователя (шлюз владеет копией, агент работает в
# рабочей копии заявки), поэтому проверку владельца нужно ослабить — но
# точечно. '*' означал бы «доверяем любому репозиторию, который окажется под
# рукой», в том числе принесённому самим агентом: git читал бы его конфиг, а
# конфиг умеет запускать команды. Список задаём заново, чтобы снять и '*' из
# прежних версий образа, и накопившиеся дубли перезапусков.
git config --system --unset-all safe.directory || true
git config --system --add safe.directory "$DATA_DIR/repo"
git config --system --add safe.directory "$DATA_DIR/worktrees/*"

# На части хостингов git по HTTP/2 рвётся на всём, что крупнее пары мегабайт:
# GET info/refs проходит, а POST git-upload-pack возвращает 401 Basic realm,
# и клон публичного репозитория выглядит как «could not read Username».
# Пойман на HostBRR: крошечный репозиторий клонируется, git/git — уже нет.
# HTTP/1.1 стоит копейки и снимает целый класс необъяснимых отказов клона.
git config --system http.version HTTP/1.1

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
