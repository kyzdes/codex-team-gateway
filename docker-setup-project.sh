#!/usr/bin/env bash
# Запускается ВНУТРИ контейнера codex-team-gateway (от имени пользователя codex)
# для клонирования вашего проекта и создания worktree на каждого коллегу.
#
# Пример вызова с хоста после `docker compose up -d` / деплоя в Dokploy:
#   docker exec -it <container_name> gosu codex \
#     bash /app/docker-setup-project.sh git@github.com:acme/myservice.git colleague1 colleague2
#
# (имена должны совпадать с ключами "users" в /config/config.json)
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Использование: $0 <git-url-проекта> <имя1> <имя2> [имя3 ...]" >&2
  exit 1
fi

REPO_URL="$1"; shift
NAMES=("$@")

PROJECTS_DIR="/home/codex/projects"
mkdir -p "${PROJECTS_DIR}"
cd "${PROJECTS_DIR}"

if [[ ! -d "project-main/.git" ]]; then
  echo "==> Клонирую ${REPO_URL} в project-main"
  git clone "${REPO_URL}" project-main
else
  echo "==> project-main уже существует, обновляю"
  git -C project-main pull
fi

cd project-main
DEFAULT_BRANCH=$(git symbolic-ref --short HEAD)

for name in "${NAMES[@]}"; do
  WORKTREE_DIR="../project-${name}"
  BRANCH="codex/${name}-work"
  if [[ -d "${WORKTREE_DIR}" ]]; then
    echo "==> ${WORKTREE_DIR} уже существует, пропускаю"
    continue
  fi
  echo "==> Создаю worktree для ${name}: ${WORKTREE_DIR} (ветка ${BRANCH})"
  git worktree add "${WORKTREE_DIR}" -b "${BRANCH}" "${DEFAULT_BRANCH}"

  if [[ -f "/app/AGENTS.md.template" ]] && [[ ! -f "${WORKTREE_DIR}/AGENTS.md" ]]; then
    cp /app/AGENTS.md.template "${WORKTREE_DIR}/AGENTS.md"
    echo "    -> скопирован AGENTS.md (заполните его под ваш сервис!)"
  fi
  if [[ -f "/app/deploy.sh.template" ]] && [[ ! -f "${WORKTREE_DIR}/deploy.sh" ]]; then
    cp /app/deploy.sh.template "${WORKTREE_DIR}/deploy.sh"
    chmod +x "${WORKTREE_DIR}/deploy.sh"
    echo "    -> скопирован deploy.sh (впишите реальные команды тестов/деплоя!)"
  fi
done

echo
echo "Готово. Рабочие директории:"
git worktree list
echo
echo "Не забудьте прописать реальные пути workdir в /config/config.json,"
echo "если имена отличаются от того, что уже сгенерировано автоматически,"
echo "и положить .env/секреты в каждую worktree-папку."
