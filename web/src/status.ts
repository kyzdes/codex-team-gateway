import type { RequestItem } from "./types";

type ChipColor = "default" | "accent" | "success" | "warning" | "danger";

export interface StatusMeta {
  label: string;
  color: ChipColor;
  /** Индекс шага в шкале STEPS; -1 — путь прерван. */
  step: number;
  busy?: boolean;
}

export const STEPS = ["Принята", "В работе", "Проверка", "Подтверждение", "Выкатка", "Готово"];

const STATUSES: Record<string, StatusMeta> = {
  queued: { label: "В очереди", color: "default", step: 0 },
  working: { label: "В работе", color: "accent", step: 1, busy: true },
  needs_input: { label: "Нужен ваш ответ", color: "warning", step: 1 },
  checking: { label: "Идёт проверка", color: "accent", step: 2, busy: true },
  tests_failed: { label: "Проверка не прошла", color: "danger", step: 2 },
  review: { label: "Ждёт подтверждения", color: "warning", step: 3 },
  merging: { label: "Применяю", color: "accent", step: 4, busy: true },
  deploying: { label: "Выкатываю на сайт", color: "accent", step: 4, busy: true },
  done: { label: "Готово", color: "success", step: 5 },
  no_changes: { label: "Без изменений", color: "default", step: 5 },
  failed: { label: "Ошибка", color: "danger", step: -1 },
  cancelled: { label: "Отменена", color: "default", step: -1 },
};

export const ATTENTION = new Set(["needs_input", "review", "done", "failed", "tests_failed"]);
export const CANCELLABLE = new Set(["queued", "working", "checking", "merging", "deploying"]);

export function statusOf(request: RequestItem): StatusMeta {
  return STATUSES[request.status] ?? { label: request.status, color: "default", step: 0 };
}

export function titleOf(request: RequestItem): string {
  return request.title || request.body.split("\n")[0];
}

export function when(iso?: string | null): string {
  if (!iso) return "";
  const date = new Date(iso);
  const diff = (Date.now() - date.getTime()) / 1000;
  if (diff < 60) return "только что";
  if (diff < 3600) return `${Math.floor(diff / 60)} мин назад`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} ч назад`;
  return date.toLocaleDateString("ru-RU", { day: "numeric", month: "long" });
}

export function clockTime(iso?: string | null): string {
  if (!iso) return "";
  return new Date(iso).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
}
