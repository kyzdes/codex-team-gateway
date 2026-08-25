import { useCallback, useEffect, useState } from "react";
import type { Dispatch, SetStateAction } from "react";
import { Alert, Button, Spinner, toast } from "@heroui/react";
import { api } from "../../api";

export interface AdminData<T> {
  data: T | null;
  error: string;
  setData: Dispatch<SetStateAction<T | null>>;
  reload: () => void;
}

/**
 * Данные вкладки. Вкладка монтируется только пока выбрана, поэтому запрос при
 * монтировании и есть «обновить»: возврат на вкладку показывает состояние
 * сервера прямо сейчас, а не то, что было в момент открытия админки.
 */
export function useAdminData<T>(path: string): AdminData<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");

  const reload = useCallback(() => {
    setError("");
    void api<T>(path)
      .then((payload) => setData(payload))
      .catch((failure: Error) => setError(failure.message));
  }, [path]);

  useEffect(reload, [reload]);

  return { data, error, setData, reload };
}

export interface Actions {
  /** Ключ выполняющегося действия — пустая строка, когда всё тихо. */
  pending: string;
  run: (key: string, action: () => Promise<void>) => void;
  /** Пропсы кнопки: крутилка на нажатой, блокировка на остальных. */
  props: (key: string) => { isDisabled: boolean; isPending: boolean };
}

/**
 * Действия админки меняют общее состояние (людей, паузу, чужую заявку), поэтому
 * выполняем строго по одному: параллельные нажатия дали бы гонку правок.
 */
export function useActions(): Actions {
  const [pending, setPending] = useState("");

  const run = useCallback((key: string, action: () => Promise<void>) => {
    setPending(key);
    void action()
      .catch((failure: Error) => toast.danger(failure.message))
      .finally(() => setPending(""));
  }, []);

  const props = useCallback(
    (key: string) => ({ isDisabled: pending !== "" && pending !== key, isPending: pending === key }),
    [pending],
  );

  return { pending, run, props };
}

/** Все действия админки — POST с необязательным телом. */
export function post<T = void>(path: string, body?: unknown): Promise<T> {
  return api<T>(path, {
    method: "POST",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

export function Loading() {
  return (
    <div className="text-muted flex items-center gap-2 py-8 text-sm">
      <Spinner className="size-4" /> Загружаю…
    </div>
  );
}

export function LoadError({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <Alert status="danger">
      <Alert.Indicator />
      <Alert.Content>
        <Alert.Title>Не удалось получить данные</Alert.Title>
        <Alert.Description>{message}</Alert.Description>
        <div className="mt-3">
          <Button size="sm" variant="secondary" onPress={onRetry}>
            Повторить
          </Button>
        </div>
      </Alert.Content>
    </Alert>
  );
}

/** Ссылку отдают человеку в мессенджер, поэтому копирование — одно нажатие. */
export function CopyButton({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);

  // Подпись «Скопировано» гасим сами: иначе кнопка навсегда остаётся в отчёте
  // об успехе и на следующей ссылке непонятно, сработала она или нет.
  useEffect(() => {
    if (!copied) return undefined;
    const timer = setTimeout(() => setCopied(false), 1500);
    return () => clearTimeout(timer);
  }, [copied]);

  return (
    <Button
      size="sm"
      variant="ghost"
      onPress={() => {
        void navigator.clipboard.writeText(value);
        setCopied(true);
      }}
    >
      {copied ? "Скопировано" : "Копировать"}
    </Button>
  );
}

/** Токены считаются сотнями тысяч — без разрядов колонку не прочитать. */
export function formatNumber(value: number): string {
  return value.toLocaleString("ru-RU");
}

/** В журнале нужна точная отметка времени, а не «час назад». */
export function formatMoment(iso?: string | null): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString("ru-RU", {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** Русские числительные: 1 день, 2 дня, 5 дней. */
export function plural(count: number, one: string, few: string, many: string): string {
  const tail = count % 100;
  if (tail >= 11 && tail <= 14) return many;
  const last = count % 10;
  if (last === 1) return one;
  if (last >= 2 && last <= 4) return few;
  return many;
}
