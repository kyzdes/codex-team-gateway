import type { StreamPayload } from "./types";

const STORAGE_KEY = "gateway_token";

let token = "";

/** Токен приходит один раз в персональной ссылке ?k=..., дальше живёт в браузере. */
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

export class Unauthorized extends Error {}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
      ...(options.headers ?? {}),
    },
  });
  if (response.status === 401) {
    forgetToken();
    throw new Unauthorized("Ссылка больше не действует");
  }
  const payload = response.status === 204 ? {} : await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error((payload as { detail?: string }).detail ?? `Ошибка ${response.status}`);
  }
  return payload as T;
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
          headers: { Authorization: `Bearer ${token}` },
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
