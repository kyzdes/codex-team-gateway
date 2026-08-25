#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Второй шаг после install.sh: клонирует ваш проект и создаёт отдельный
# git worktree под каждого коллегу, чтобы параллельные сессии Codex не
# конфликтовали по файлам.
#
# Использование:
#   sudo -u codex ./setup-project.sh <git-url-вашего-проекта> colleague1 colleague2 [colleague3 ...]
#
# Пример:
#   sudo -u codex ./setup-project.sh git@github.com:acme/myservice.git alice bob
# ---------------------------------------------------------------------------
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Использование: $0 <git-url-проекта> <имя1> <имя2> [имя3 ...]" >&2
  echo "Имена должны совпадать с ключами users в /etc/codex-gateway/config.json" >&2
  exit 1
fi

REPO_URL="$1"; shift
NAMES=("$@")

PROJECTS_DIR="${PROJECTS_DIR:-$HOME/projects}"
mkdir -p "${PROJECTS_DIR}"
cd "${PROJECTS_DIR}"

if [[ ! -d "project-main/.git" ]]; then
  echo "==> Клонируем ${REPO_URL} в project-main"
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

  if [[ -f "AGENTS.md.template" ]] && [[ ! -f "${WORKTREE_DIR}/AGENTS.md" ]]; then
    cp AGENTS.md.template "${WORKTREE_DIR}/AGENTS.md"
    echo "    -> скопирован AGENTS.md (заполните его под ваш сервис!)"
  fi
  if [[ -f "deploy.sh.template" ]] && [[ ! -f "${WORKTREE_DIR}/deploy.sh" ]]; then
    cp deploy.sh.template "${WORKTREE_DIR}/deploy.sh"
    chmod +x "${WORKTREE_DIR}/deploy.sh"
    echo "    -> скопирован deploy.sh (впишите реальные команды тестов/деплоя!)"
  fi
done

echo
echo "Готово. Рабочие директории:"
git worktree list
echo
echo "Не забудьте:"
echo "  1) прописать реальные пути workdir в /etc/codex-gateway/config.json"
echo "  2) положить .env/секреты в каждую из папок (или симлинком на общий защищённый файл)"
echo "  3) заполнить AGENTS.md и deploy.sh в каждой worktree-папке"
echo "  4) перезапустить шлюз: sudo systemctl restart codex-gateway"
