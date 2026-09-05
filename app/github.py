"""GitHub REST: открыть PR, следить за проверками, смержить.

Токен принадлежит шлюзу. Агент его не видит и физически не может смержить
что-то сам — мерж происходит только по нажатию человека в интерфейсе, и
поверх него всё равно работает branch protection репозитория.
"""

from __future__ import annotations

from typing import Any

import httpx

from .config import github_token, settings

API = "https://api.github.com"


class GitHubError(RuntimeError):
    """Отказ GitHub. Код ответа держим отдельным полем: по нему отличают
    «нет такого права» (403/404) от «GitHub прилёг» (5xx), а разбирать это
    из текста значило бы гадать по чужой формулировке."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def _request(method: str, path: str, *, token: str | None = None, **kwargs: Any) -> Any:
    """Запрос к GitHub действующим токеном шлюза.

    `token` передают только там, где проверяют ещё не сохранённого кандидата:
    в остальных случаях источник один — config.github_token().
    """
    auth = token if token is not None else github_token()
    if not auth:
        raise GitHubError(
            "GITHUB_TOKEN не задан — обращаться к GitHub нечем: "
            "впишите токен в админке или задайте переменную окружения"
        )
    url = f"{API}{path}"
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.request(method, url, headers=_headers(auth), **kwargs)
    if response.status_code >= 400:
        detail = ""
        try:
            payload = response.json()
            detail = payload.get("message", "")
            for err in payload.get("errors", []) or []:
                if isinstance(err, dict) and err.get("message"):
                    detail += f" — {err['message']}"
        except ValueError:
            detail = response.text[:300]
        raise GitHubError(f"GitHub {response.status_code}: {detail}", response.status_code)
    if response.status_code == 204 or not response.content:
        return {}
    return response.json()


async def create_pull_request(branch: str, title: str, body: str) -> dict[str, Any]:
    pr = await _request(
        "POST",
        f"/repos/{settings.repo}/pulls",
        json={"title": title, "head": branch, "base": settings.base_branch, "body": body},
    )
    if settings.pr_labels:
        try:
            await _request(
                "POST",
                f"/repos/{settings.repo}/issues/{pr['number']}/labels",
                json={"labels": list(settings.pr_labels)},
            )
        except GitHubError:
            pass  # метки не критичны
    return {"number": pr["number"], "url": pr["html_url"], "head_sha": pr["head"]["sha"]}


async def find_open_pull_request(branch: str) -> dict[str, Any] | None:
    items = await _request(
        "GET",
        f"/repos/{settings.repo}/pulls",
        params={"head": f"{settings.repo_owner}:{branch}", "state": "open"},
    )
    if not items:
        return None
    pr = items[0]
    return {"number": pr["number"], "url": pr["html_url"], "head_sha": pr["head"]["sha"]}


async def check_state(sha: str) -> dict[str, Any]:
    """Состояние обязательной проверки: pending | success | failure | missing."""
    runs = await _request("GET", f"/repos/{settings.repo}/commits/{sha}/check-runs")
    wanted = settings.required_check.lower()
    matched = [r for r in runs.get("check_runs", []) if wanted in (r.get("name") or "").lower()]

    if not matched:
        # Проверка может приходить как commit status, а не как check run.
        combined = await _request("GET", f"/repos/{settings.repo}/commits/{sha}/status")
        for status in combined.get("statuses", []):
            if wanted in (status.get("context") or "").lower():
                state = status.get("state")
                return {
                    "status": {"success": "success", "pending": "pending"}.get(state, "failure"),
                    "detail": status.get("description") or "",
                    "url": status.get("target_url") or "",
                }
        total = len(runs.get("check_runs", []))
        return {
            "status": "missing",
            "detail": f"проверка «{settings.required_check}» пока не запускалась" if total == 0 else "",
            "url": "",
        }

    run = matched[0]
    if run.get("status") != "completed":
        return {"status": "pending", "detail": "проверка идёт", "url": run.get("html_url", "")}
    conclusion = run.get("conclusion")
    if conclusion == "success":
        return {"status": "success", "detail": "", "url": run.get("html_url", "")}
    return {
        "status": "failure",
        "detail": f"проверка завершилась статусом {conclusion}",
        "url": run.get("html_url", ""),
    }


async def pull_request_state(number: int) -> dict[str, Any]:
    pr = await _request("GET", f"/repos/{settings.repo}/pulls/{number}")
    return {
        "merged": bool(pr.get("merged")),
        "state": pr.get("state"),
        "mergeable": pr.get("mergeable"),
        "mergeable_state": pr.get("mergeable_state"),
        "head_sha": pr["head"]["sha"],
    }


async def merge(number: int, title: str) -> str:
    """Мерж в main. Все правила репозитория (зелёные проверки, обсуждения)
    остаются в силе — GitHub откажет, если они не выполнены."""
    result = await _request(
        "PUT",
        f"/repos/{settings.repo}/pulls/{number}/merge",
        json={"merge_method": settings.merge_method, "commit_title": title},
    )
    return result.get("sha", "")


async def close_pull_request(number: int) -> None:
    await _request("PATCH", f"/repos/{settings.repo}/pulls/{number}", json={"state": "closed"})


async def pull_requests_readable(token: str | None = None) -> bool | None:
    """Виден ли токену список pull request-ов.

    Отдельным запросом, потому что в `permissions` репозитория этого права
    просто нет: там {admin, maintain, push, triage, pull} — про Contents, а
    Pull requests у fine-grained PAT выдаются отдельным переключателем.
    Заявка без него доходит до самого конца и падает на create_pull_request,
    когда прогон агента уже оплачен, а рабочая копия вот-вот будет снесена.

    Ответ трёхзначный: True — список отдали, False — прав нет, None — GitHub
    ответил чем-то другим (лежит, лимит), и объявлять это виной ключа нельзя.
    """
    try:
        await _request(
            "GET", f"/repos/{settings.repo}/pulls", token=token,
            params={"state": "all", "per_page": 1},
        )
    except GitHubError as exc:
        # 404 здесь — тот же отказ в доступе: GitHub прячет то, чего токену
        # не положено видеть, вместо честного 403.
        return False if exc.status in (403, 404) else None
    return True


async def token_check(token: str | None = None) -> dict[str, Any]:
    """Проверка доступа для админки: видим ли репозиторий, можем ли писать и
    пускают ли нас к pull request-ам.

    Без аргумента проверяется действующий токен (чек-лист готовности), с
    аргументом — кандидат, которого администратор только что ввёл: класть в
    базу непроверенное значение значит чинить доступ вслепую.

    pull_requests проверяет доступ к pull request-ам чтением: право на запись
    в них без создания настоящего PR не проверить ничем. Зато так ловится
    главный случай — переключатель Pull requests не тронут вовсе.
    """
    try:
        repo = await _request("GET", f"/repos/{settings.repo}", token=token)
    except GitHubError as exc:
        return {"ok": False, "error": str(exc)}
    permissions = repo.get("permissions") or {}
    return {
        "ok": True,
        "repo": repo.get("full_name"),
        "private": repo.get("private"),
        "can_push": bool(permissions.get("push")),
        "pull_requests": await pull_requests_readable(token),
        "default_branch": repo.get("default_branch"),
    }
