"""Конфигурация инстанса.

Один инстанс = один проект одного клиента. Всё, что специфично для проекта,
приходит из переменных окружения (Dokploy → Environment), а не из кода —
это и делает репозиторий шаблоном.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "да"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except ValueError:
        return default


def _env_choice(name: str, allowed: set[str], default: str) -> str:
    """Значение из фиксированного набора.

    Опечатка в переменной не должна превращаться в третий, никому не известный
    режим работы: тот, кто читает настройку, вправе рассчитывать ровно на один
    из перечисленных вариантов и не проверять её ещё раз.
    """
    value = _env(name).lower()
    return value if value in allowed else default


# Обёртка, через которую шлюз запускает что-либо от имени пользователя-агента
# (docker/run-agent.sh). Путь фиксирован образом и намеренно не берётся из
# окружения: настраиваемый путь означал бы «запусти под sudo что попросят».
AGENT_WRAPPER = "/usr/local/bin/run-agent.sh"


@dataclass(frozen=True)
class Settings:
    # --- Брендирование (клиент видит это в интерфейсе) -----------------
    brand_name: str = field(default_factory=lambda: _env("BRAND_NAME", "Правки"))
    brand_subtitle: str = field(
        default_factory=lambda: _env("BRAND_SUBTITLE", "изменения на сайте без разработчика")
    )
    brand_accent: str = field(default_factory=lambda: _env("BRAND_ACCENT", "#3f6fff"))

    # --- Проект ---------------------------------------------------------
    repo: str = field(default_factory=lambda: _env("PROJECT_REPO"))  # "owner/name"
    base_branch: str = field(default_factory=lambda: _env("PROJECT_BASE_BRANCH", "main"))
    prod_url: str = field(default_factory=lambda: _env("PROJECT_PROD_URL").rstrip("/"))
    health_path: str = field(default_factory=lambda: _env("PROJECT_HEALTH_PATH", "/healthz"))

    # Локальный прогон тестов до пуша. Пусто = не гоняем и полагаемся на
    # GitHub Actions (для этого в контейнере должен стоять тулчейн проекта).
    test_cmd: str = field(default_factory=lambda: _env("PROJECT_TEST_CMD"))
    test_timeout: int = field(default_factory=lambda: _env_int("PROJECT_TEST_TIMEOUT", 900))

    # --- GitHub ---------------------------------------------------------
    # Токен принадлежит ШЛЮЗУ, а не Codex: агент коммитит локально, а пуш,
    # PR и мерж делает сервер по действию человека в интерфейсе.
    github_token: str = field(default_factory=lambda: _env("GITHUB_TOKEN"))
    required_check: str = field(default_factory=lambda: _env("GITHUB_REQUIRED_CHECK", "tests"))
    merge_method: str = field(default_factory=lambda: _env("GITHUB_MERGE_METHOD", "squash"))
    pr_labels: tuple[str, ...] = field(
        default_factory=lambda: tuple(x.strip() for x in _env("GITHUB_PR_LABELS").split(",") if x.strip())
    )

    # --- Codex ----------------------------------------------------------
    codex_bin: str = field(default_factory=lambda: _env("CODEX_BIN", "codex"))
    # От имени какого пользователя запускать Codex. Пусто = тем же, что и шлюз
    # (годится для локальной разработки, но тогда агент видит секреты шлюза).
    agent_user: str = field(default_factory=lambda: _env("AGENT_USER", "agent"))
    codex_sandbox: str = field(default_factory=lambda: _env("CODEX_SANDBOX", "workspace-write"))
    codex_network: bool = field(default_factory=lambda: _env_bool("CODEX_NETWORK_ACCESS", True))
    codex_model: str = field(default_factory=lambda: _env("CODEX_MODEL"))
    codex_timeout: int = field(default_factory=lambda: _env_int("CODEX_TIMEOUT", 3600))
    max_concurrent: int = field(default_factory=lambda: _env_int("MAX_CONCURRENT_RUNS", 2))
    # Осознанное разрешение работать без отдельного пользователя агента.
    # По умолчанию выключено: без изоляции дочерний процесс читает
    # /proc/<pid шлюза>/environ и достаёт оттуда всё, что вычистил clean_env().
    # Нужно ровно для локальной разработки и тестов.
    allow_unisolated_agent: bool = field(
        default_factory=lambda: _env_bool("ALLOW_UNISOLATED_AGENT", False)
    )

    # --- Dokploy (опционально: показывать статус выкатки) ---------------
    dokploy_url: str = field(default_factory=lambda: _env("DOKPLOY_URL").rstrip("/"))
    dokploy_token: str = field(default_factory=lambda: _env("DOKPLOY_TOKEN"))
    dokploy_application_id: str = field(default_factory=lambda: _env("DOKPLOY_APPLICATION_ID"))
    deploy_timeout: int = field(default_factory=lambda: _env_int("DEPLOY_TIMEOUT", 900))

    # --- Уведомления (опционально) --------------------------------------
    telegram_token: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN"))

    # --- Правила приёма и подтверждения ---------------------------------
    # Кто подтверждает выкатку: "user" — автор заявки, "admin" — только
    # администратор. Второй режим нужен там, где сотруднику нельзя давать
    # последнее слово перед продом.
    approval_policy: str = field(
        default_factory=lambda: _env_choice("APPROVAL_POLICY", {"user", "admin"}, "user")
    )
    # Каждая заявка — это оплаченный прогон агента, поэтому «нажал десять раз,
    # потому что не понял, приняли ли» не должно стоить десяти прогонов.
    # 0 — снять ограничение.
    rate_limit_per_hour: int = field(default_factory=lambda: _env_int("RATE_LIMIT_PER_HOUR", 20))
    # Потолок черновиков картинок на человека: забытые скриншоты иначе съедают volume.
    upload_quota_mb: int = field(default_factory=lambda: _env_int("UPLOAD_QUOTA_MB", 64))
    # Сколько дней держим логи агента и картинки уже завершённых заявок.
    retention_days: int = field(default_factory=lambda: _env_int("RETENTION_DAYS", 60))

    # --- Данные ---------------------------------------------------------
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", "/data")))

    @property
    def repo_dir(self) -> Path:
        return self.data_dir / "repo"

    @property
    def worktrees_dir(self) -> Path:
        return self.data_dir / "worktrees"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def users_path(self) -> Path:
        return self.data_dir / "users.json"

    @property
    def agent_wrapper(self) -> str:
        return AGENT_WRAPPER

    @property
    def repo_owner(self) -> str:
        return self.repo.split("/", 1)[0] if "/" in self.repo else ""

    @property
    def repo_name(self) -> str:
        return self.repo.split("/", 1)[1] if "/" in self.repo else self.repo

    @property
    def clone_url(self) -> str:
        # PROJECT_GIT_URL нужен для локальных прогонов и самохостед-git.
        return _env("PROJECT_GIT_URL") or f"https://github.com/{self.repo}.git"

    @property
    def health_url(self) -> str:
        if not self.prod_url:
            return ""
        return self.prod_url + (self.health_path if self.health_path.startswith("/") else "/" + self.health_path)

    def problems(self) -> list[str]:
        """Проверки, без которых инстанс работать не будет."""
        out: list[str] = []
        if not self.repo or "/" not in self.repo:
            out.append("PROJECT_REPO не задан или не в формате owner/name")
        if not self.github_token:
            out.append("GITHUB_TOKEN не задан — шлюз не сможет пушить ветки и открывать PR")
        if self.codex_sandbox not in {"read-only", "workspace-write", "danger-full-access"}:
            out.append(f"CODEX_SANDBOX={self.codex_sandbox!r} — недопустимое значение")
        if self.merge_method not in {"merge", "squash", "rebase"}:
            out.append(f"GITHUB_MERGE_METHOD={self.merge_method!r} — допустимо merge|squash|rebase")
        # Аргументы команды видны любому процессу контейнера через
        # /proc/<pid>/cmdline, поэтому учётке в адресе репозитория там не место.
        host = self.clone_url.split("//", 1)[-1].split("/", 1)[0]
        if "@" in host:
            out.append(
                "PROJECT_GIT_URL содержит логин или токен в адресе — он попадёт в "
                "аргументы git clone, а их читает любой процесс контейнера"
            )
        raw_policy = _env("APPROVAL_POLICY")
        if raw_policy and raw_policy.lower() != self.approval_policy:
            out.append(
                f"APPROVAL_POLICY={raw_policy!r} — допустимо user|admin, "
                f"сейчас действует {self.approval_policy}"
            )
        return out


settings = Settings()

for _d in (settings.data_dir, settings.repo_dir.parent, settings.worktrees_dir, settings.logs_dir):
    _d.mkdir(parents=True, exist_ok=True)
