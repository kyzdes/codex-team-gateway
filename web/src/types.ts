export interface TextChange {
  file: string;
  before: string;
  after: string;
}

/** Расход модели по заявке; складывается на сервере за все прогоны агента. */
export interface Usage {
  input_tokens?: number;
  cached_input_tokens?: number;
  output_tokens?: number;
  turns?: number;
}

export interface RequestItem {
  id: number;
  user: string;
  author?: string;
  status: string;
  body: string;
  title?: string | null;
  summary?: string | null;
  user_visible: string[];
  notes?: string | null;
  risk?: string | null;
  question?: string | null;
  branch?: string | null;
  pr_number?: number | null;
  pr_url?: string | null;
  checks_status?: string | null;
  checks_detail?: string | null;
  files: string[];
  text_changes: TextChange[];
  images: string[];
  usage?: Usage;
  approved_by?: string | null;
  approved_at?: string | null;
  error?: string | null;
  created_at: string;
  updated_at: string;
  merged_at?: string | null;
  deployed_at?: string | null;
}

export interface EventItem {
  request_id?: number;
  ts: string;
  kind: string;
  text: string;
}

export interface Me {
  login: string;
  display_name: string;
  role: string;
  brand: { name: string; subtitle: string; accent: string };
  project: { site: string; repo: string };
}

/** Набор оттенков совпадает с палитрой чипов HeroUI. */
export type StatusTone = "default" | "accent" | "success" | "warning" | "danger";

export interface StatusMetaPayload {
  label: string;
  tone: StatusTone;
  /** Индекс шага в шкале steps; -1 — путь прерван. */
  step: number;
  busy?: boolean;
}

/** Ответ /api/meta: подписи статусов и ограничения инстанса. */
export interface MetaPayload {
  statuses: Record<string, StatusMetaPayload>;
  steps: string[];
  limits: { max_images: number; rate_limit_per_hour: number };
  approval_policy: string;
  paused: boolean;
}

/** Пункт чек-листа готовности инстанса (/api/admin/readiness). */
export interface ReadinessCheck {
  key: string;
  title: string;
  ok: boolean;
  detail: string;
  hint: string;
}

export interface ReadinessResponse {
  checks: ReadinessCheck[];
  /** Ошибки в переменных окружения самого шлюза — их не видит ни одна проверка выше. */
  problems: string[];
}

/**
 * Откуда шлюз берёт токен GitHub. Значение из интерфейса важнее переменной
 * окружения: администратор меняет его без редеплоя, env остаётся затравкой.
 */
export type GithubTokenSource = "ui" | "env" | "none";

/**
 * Состояние доступа к GitHub (/api/admin/github-token).
 *
 * Самого токена здесь нет и не будет: наружу отдаётся только хвост из четырёх
 * символов — его хватает, чтобы отличить один ключ от другого, и не хватает,
 * чтобы им воспользоваться.
 */
export interface GithubTokenState {
  configured: boolean;
  source: GithubTokenSource;
  /** Хвост вида «…f3a2»; пустая строка, когда токена нет вовсе. */
  hint: string;
  /** Репозиторий, на который должен быть выдан PAT. */
  repo: string;
  /** Итог боевой проверки; null — токен ещё ни разу не проверяли. */
  can_push: boolean | null;
  checked_at: string | null;
  /** Чем закончилась последняя проверка, если она сорвалась. */
  error: string | null;
}

/** Строка расхода токенов по человеку (/api/admin/usage). */
export interface UsageRow {
  user: string;
  /** Человеческое имя; у людей, удалённых до появления таблицы people, его нет. */
  author?: string;
  requests: number;
  input_tokens: number;
  output_tokens: number;
}

export interface UsageResponse {
  totals: UsageRow[];
  days: number;
}

/** Запись журнала подтверждений выкатки (/api/admin/journal). */
export interface JournalEntry {
  id: number;
  title: string;
  user: string;
  /** Имена вместо логинов: журнал читают люди, а не машины. */
  author?: string;
  approver?: string;
  approved_by: string;
  approved_at: string;
  pr_url?: string | null;
  status: string;
}

export interface JournalResponse {
  entries: JournalEntry[];
}

/** Человек из таблицы people (/api/admin/people). */
export interface Person {
  login: string;
  display_name: string;
  role: string;
  disabled: boolean;
  link: string;
}

export interface PeopleResponse {
  people: Person[];
}

export type StreamPayload =
  | { type: "hello" }
  | { type: "request"; request: RequestItem }
  | ({ type: "event" } & EventItem);
