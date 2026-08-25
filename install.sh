#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# codex-team-gateway installer
#
# Ставит на чистую Ubuntu (Mac mini или любой VPS) всё необходимое для
# совместной работы команды с Codex CLI: Node.js, сам Codex CLI, Python
# и зависимости веб-шлюза (codex-team-gateway), systemd-сервис.
#
# Использование:
#   curl -fsSL https://raw.githubusercontent.com/<user>/codex-team-gateway/main/install.sh | sudo bash
#
# или локально:
#   git clone https://github.com/<user>/codex-team-gateway.git
#   cd codex-team-gateway && sudo ./install.sh
#
# Переменные окружения (необязательные, можно переопределить перед запуском):
#   INSTALL_DIR         куда ставить шлюз      (по умолчанию /opt/codex-team-gateway)
#   SERVICE_USER         системный пользователь (по умолчанию codex)
#   GATEWAY_PORT         порт шлюза             (по умолчанию 8787)
#   REPO_URL             откуда клонировать код (по умолчанию этот репозиторий)
# ---------------------------------------------------------------------------
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/codex-team-gateway}"
SERVICE_USER="${SERVICE_USER:-codex}"
GATEWAY_PORT="${GATEWAY_PORT:-8787}"
REPO_URL="${REPO_URL:-https://github.com/kyzdes/codex-team-gateway.git}"
CONFIG_DIR="/etc/codex-gateway"

log() { echo -e "\033[1;34m==>\033[0m $1"; }

if [[ "${EUID}" -ne 0 ]]; then
  echo "Запустите с sudo: sudo ./install.sh (или sudo bash install.sh)" >&2
  exit 1
fi

log "Обновляем списки пакетов и ставим системные зависимости"
apt-get update -y
apt-get install -y git curl build-essential python3-venv python3-pip ufw

log "Устанавливаем Node.js LTS (нужен для Codex CLI)"
if ! command -v node >/dev/null 2>&1; then
  curl -fsSL https://deb.nodesource.com/setup_lts.x | bash -
  apt-get install -y nodejs
else
  log "Node.js уже установлен: $(node -v)"
fi

log "Устанавливаем Codex CLI"
npm install -g @openai/codex

log "Создаём системного пользователя ${SERVICE_USER} (если его ещё нет)"
if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd -m -s /bin/bash "${SERVICE_USER}"
fi

log "Разворачиваем код шлюза в ${INSTALL_DIR}"
if [[ -d "${INSTALL_DIR}/.git" ]]; then
  sudo -u "${SERVICE_USER}" git -C "${INSTALL_DIR}" pull
elif [[ -f "./app.py" ]]; then
  # Запуск из уже склонированного репозитория — просто копируем на место
  mkdir -p "${INSTALL_DIR}"
  cp -r ./* "${INSTALL_DIR}/"
  chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"
else
  sudo -u "${SERVICE_USER}" git clone "${REPO_URL}" "${INSTALL_DIR}"
fi

log "Создаём виртуальное окружение Python и ставим зависимости шлюза"
sudo -u "${SERVICE_USER}" python3 -m venv "${INSTALL_DIR}/venv"
sudo -u "${SERVICE_USER}" "${INSTALL_DIR}/venv/bin/pip" install --upgrade pip -q
sudo -u "${SERVICE_USER}" "${INSTALL_DIR}/venv/bin/pip" install -q -r "${INSTALL_DIR}/requirements.txt"

log "Готовим конфиг шлюза (${CONFIG_DIR}/config.json)"
mkdir -p "${CONFIG_DIR}"
if [[ ! -f "${CONFIG_DIR}/config.json" ]]; then
  TOKEN1=$(openssl rand -hex 24)
  TOKEN2=$(openssl rand -hex 24)
  cat > "${CONFIG_DIR}/config.json" <<EOF
{
  "sandbox_mode": "danger-full-access",
  "approval_policy": "never",
  "model": null,
  "users": {
    "colleague1": {
      "token": "${TOKEN1}",
      "workdir": "/home/${SERVICE_USER}/projects/project-colleague1",
      "display_name": "Коллега 1"
    },
    "colleague2": {
      "token": "${TOKEN2}",
      "workdir": "/home/${SERVICE_USER}/projects/project-colleague2",
      "display_name": "Коллега 2"
    }
  }
}
EOF
  log "Сгенерированы токены доступа (сохраните их — больше не выведутся):"
  echo "    colleague1: ${TOKEN1}"
  echo "    colleague2: ${TOKEN2}"
else
  log "Конфиг уже существует, не перезаписываю: ${CONFIG_DIR}/config.json"
fi

mkdir -p "/home/${SERVICE_USER}/projects" "${INSTALL_DIR}/state" "${INSTALL_DIR}/logs"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "/home/${SERVICE_USER}/projects" "${INSTALL_DIR}/state" "${INSTALL_DIR}/logs"

log "Устанавливаем systemd-сервис"
sed \
  -e "s#/opt/codex-team-gateway#${INSTALL_DIR}#g" \
  -e "s#User=codex#User=${SERVICE_USER}#g" \
  -e "s#--port 8787#--port ${GATEWAY_PORT}#g" \
  "${INSTALL_DIR}/systemd/codex-gateway.service" > /etc/systemd/system/codex-gateway.service

systemctl daemon-reload
systemctl enable --now codex-gateway

log "Закрываем порт шлюза от внешнего мира (доступ только через Tailscale/локально)"
ufw allow OpenSSH >/dev/null 2>&1 || true
ufw --force enable >/dev/null 2>&1 || true

echo
log "Готово. Шлюз запущен: systemctl status codex-gateway"
echo "  Локальный адрес:   http://127.0.0.1:${GATEWAY_PORT}"
echo "  Конфиг токенов:    ${CONFIG_DIR}/config.json"
echo "  Логи запросов:     ${INSTALL_DIR}/logs/"
echo
echo "Дальше:"
echo "  1) codex login --device-auth   (под пользователем ${SERVICE_USER})"
echo "  2) настроить git worktree для каждого коллеги в /home/${SERVICE_USER}/projects/"
echo "  3) заполнить AGENTS.md.template и deploy.sh.template под свой сервис"
echo "  4) поднять Tailscale для удалённого доступа: curl -fsSL https://tailscale.com/install.sh | sh && tailscale up"
echo
echo "Подробная инструкция: README.md в этом репозитории"
