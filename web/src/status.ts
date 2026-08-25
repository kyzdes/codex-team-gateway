import { api } from "./api";
import type { MetaPayload, RequestItem, StatusTone } from "./types";

export interface StatusMeta {
  label: string;
  /** Сервер зовёт это поле tone, HeroUI — color; переводим при загрузке. */
  color: StatusTone;
  /** Индекс шага в шкале steps(); -1 — путь прерван. */
  step: number;
  busy?: boolean;
}

/**
 * Запасные значения. Единственный источник правды по статусам — сервер
 * (/api/meta), но интерфейс не должен пустеть, пока ответ не пришёл или если
 * его не будет вовсе: на старой версии шлюза человек увидит те же подписи.
 */
const FALLBACK_STEPS: string[] = [
  "Принята",
  "В работе",
  "Проверка",
  "Подтверждение",
  "Выкатка",
  "Готово",
];

const FALLBACK_STATUSES: Record<string, StatusMeta> = {
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

let stepLabels: string[] = FALLBACK_STEPS;
let statuses: Record<string, StatusMeta> = FALLBACK_STATUSES;
let approval = "user";
let intakePaused = false;
let imageLimit = 4;

/**
 * Подтягивает описание статусов с сервера. Ошибку глотаем намеренно: без
 * /api/meta заявки всё равно показываются, просто на запасных подписях.
 */
export async function loadMeta(): Promise<void> {
  try {
    const meta = await api<MetaPayload>("/api/meta");
    if (meta.steps?.length) stepLabels = meta.steps;
    if (meta.statuses && Object.keys(meta.statuses).length) {
      statuses = Object.fromEntries(
        Object.entries(meta.statuses).map(([key, item]) => [
          key,
          { label: item.label, color: item.tone, step: item.step, busy: item.busy },
        ]),
      );
    }
    if (meta.approval_policy) approval = meta.approval_policy;
    if (meta.limits?.max_images) imageLimit = meta.limits.max_images;
    intakePaused = Boolean(meta.paused);
  } catch {
    /* сервер не ответил — остаёмся на запасных значениях */
  }
}

export function steps(): string[] {
  return stepLabels;
}

/** "admin" — выкатку подтверждает только администратор. */
export function approvalPolicy(): string {
  return approval;
}

export function isPaused(): boolean {
  return intakePaused;
}

/** Сколько картинок сервер примет к одному сообщению. Он же их и отбивает. */
export function maxImages(): number {
  return imageLimit;
}

export const ATTENTION = new Set(["needs_input", "review", "done", "failed", "tests_failed"]);
export const CANCELLABLE = new Set(["queued", "working", "checking", "merging", "deploying"]);
export const TERMINAL = new Set(["done", "failed", "cancelled", "no_changes", "tests_failed"]);
/** Где сервер согласится переспросить проверки GitHub; на остальном — 409. */
export const RECHECKABLE = new Set(["checking", "tests_failed"]);

/** Русское склонение по числу: «1 прогон», «2 прогона», «5 прогонов». */
export function plural(count: number, one: string, few: string, many: string): string {
  const tail = count % 10;
  const hundred = count % 100;
  if (tail === 1 && hundred !== 11) return one;
  if (tail >= 2 && tail <= 4 && (hundred < 12 || hundred > 14)) return few;
  return many;
}

export function statusOf(request: RequestItem): StatusMeta {
  return statuses[request.status] ?? { label: request.status, color: "default", step: 0 };
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
