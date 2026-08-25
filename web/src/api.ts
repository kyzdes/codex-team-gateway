import type { StreamPayload } from "./types";

const STORAGE_KEY = "gateway_token";

let token = "";

/**
 * Токен приходит один раз в персональной ссылке ?k=..., дальше живёт в браузере.
 * Обычно ?k= забирает сам сервер (ставит cookie-сессию и редиректит на "/"),
 * но старые закладки и прямые ссылки на файл всё ещё приносят токен сюда.
 */
export function initToken(): string {
  const url = new URL(window.location.href);
  const fromLink = url.searchParams.get("k");
  if (fromLink) {
    localStorage.setItem(STORAGE_KEY, fromLink);
    url.searchParams.delete("k");
    window.history.replaceState({}, "", url.pathname + url.search);
  }
  token = localStorage.getItem(STORAGE_KEY) ?? "";
  return token;
}

export function forgetToken(): void {
  localStorage.removeItem(STORAGE_KEY);
  token = "";
}

/**
 * Заголовок ставим только при живом токене: без него запрос авторизует
 * cookie-сессия, а пустой `Bearer ` сервер честно считает неверным токеном.
 */
function authHeader(): Record<string, string> {
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export class Unauthorized extends Error {}

/**
 * Человеческий текст ошибки из ответа сервера. FastAPI отдаёт detail строкой
 * почти везде, но на 422 это массив описаний полей — подставленный в текст как
 * есть, он превращался в «[object Object]», и ровно это видел человек, вставив
 * в заявку слишком длинное письмо.
 */
function explain(payload: unknown, status: number): string {
  const detail = (payload as { detail?: unknown })?.detail;
  if (typeof detail === "string" && detail) return detail;
  if (status === 422) return "Текст не подошёл: он слишком длинный или слишком короткий.";
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => (item as { msg?: string })?.msg)
      .filter((msg): msg is string => Boolean(msg));
    if (messages.length) return messages.join("; ");
  }
  return `Ошибка ${status}`;
}

/**
 * Протухший токен не должен перебивать живую cookie-сессию.
 *
 * Администратор перевыпускает человеку ссылку, тот открывает новую — сервер
 * ставит свежую cookie и редиректит, а в localStorage лежит старый токен.
 * Отправив его, мы получали 401 и показывали «ссылка не действует» поверх
 * совершенно рабочего входа. Поэтому при первом отказе выбрасываем токен и
 * повторяем запрос ровно один раз уже без него.
 */
async function withRetry(attempt: (bearer: string) => Promise<Response>): Promise<Response> {
  const response = await attempt(token);
  if (response.status !== 401 || !token) return response;
  forgetToken();
  return attempt("");
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await withRetry((bearer) =>
    fetch(path, {
      ...options,
      headers: {
        "Content-Type": "application/json",
        ...(bearer ? { Authorization: `Bearer ${bearer}` } : {}),
        ...(options.headers ?? {}),
      },
    }),
  );
  if (response.status === 401) {
    forgetToken();
    throw new Unauthorized("Ссылка больше не действует");
  }
  const payload = response.status === 204 ? {} : await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(explain(payload, response.status));
  return payload as T;
}

/** Загрузка картинки: FormData сам проставит boundary, Content-Type не трогаем. */
export async function uploadImage(file: File): Promise<string> {
  const form = new FormData();
  form.append("file", file);
  const response = await withRetry((bearer) =>
    fetch("/api/uploads", {
      method: "POST",
      headers: bearer ? { Authorization: `Bearer ${bearer}` } : {},
      body: form,
    }),
  );
  if (response.status === 401) {
    forgetToken();
    throw new Unauthorized("Ссылка больше не действует");
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(explain(payload, response.status));
  return (payload as { id: string }).id;
}

/** Картинки закрыты токеном, поэтому тянем их запросом и показываем как blob. */
export async function fetchImage(path: string): Promise<string> {
  const response = await fetch(path, { headers: authHeader() });
  if (!response.ok) throw new Error("Картинка недоступна");
  return URL.createObjectURL(await response.blob());
}

/**
 * Живая лента событий. EventSource не умеет заголовки, поэтому читаем поток
 * сами — так токен не попадает в адресную строку и в логи сервера.
 */
export function listen(onPayload: (payload: StreamPayload) => void): () => void {
  const controller = new AbortController();

  (async () => {
    while (!controller.signal.aborted) {
      try {
        const response = await fetch("/api/stream", {
          headers: authHeader(),
          signal: controller.signal,
        });
        if (!response.ok || !response.body) throw new Error("stream");
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (!controller.signal.aborted) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const chunks = buffer.split("\n\n");
          buffer = chunks.pop() ?? "";
          for (const chunk of chunks) {
            const line = chunk.split("\n").find((item) => item.startsWith("data: "));
            if (line) onPayload(JSON.parse(line.slice(6)) as StreamPayload);
          }
        }
      } catch {
        /* обрыв связи — подождём и переподключимся */
      }
      if (controller.signal.aborted) return;
      await new Promise((resolve) => setTimeout(resolve, 3000));
    }
  })();

  return () => controller.abort();
}
