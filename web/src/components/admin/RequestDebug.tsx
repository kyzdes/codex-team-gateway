import { useCallback, useEffect, useState } from "react";
import { Button, Disclosure, Spinner, toast } from "@heroui/react";
import { api } from "../../api";
import { RECHECKABLE, TERMINAL, plural } from "../../status";
import type { Usage } from "../../types";
import { Section } from "../Steps";
import { post, useActions, useAdminData } from "./shared";

interface TestsPayload {
  output: string;
}

interface LogPayload {
  lines: string[];
}

export interface RequestDebugProps {
  requestId: number;
  /** Статус заявки: снимать принудительно уже завершённую нечего. */
  status: string;
  /** Без PR опрашивать в GitHub тоже нечего — сервер на это отвечает 409. */
  prNumber?: number | null;
  /** Что заявка стоила: сложено по всем прогонам агента. */
  usage?: Usage;
}

/**
 * Админский блок карточки заявки. Показывает то, что раньше жило только в
 * консоли сервера: вывод упавших тестов и сырой лог агента. Плюс два рычага на
 * случай зависшего прогона — снять заявку и переспросить проверки GitHub.
 *
 * За данными ходит сам: это единственное место, где они нужны, и тащить их
 * через все пропсы карточки ради двух кнопок не за что.
 */
export function RequestDebug({ requestId, status, prNumber, usage }: RequestDebugProps) {
  const tests = useAdminData<TestsPayload>(`/api/admin/requests/${requestId}/tests`);
  const [log, setLog] = useState<string[] | null>(null);
  const [logError, setLogError] = useState("");
  const [logOpen, setLogOpen] = useState(false);
  const actions = useActions();

  // Переключение на другую заявку не должно оставлять чужой лог на экране.
  useEffect(() => {
    setLog(null);
    setLogError("");
    setLogOpen(false);
  }, [requestId]);

  // Лог тянем только по требованию: это сотни строк, которые почти никогда не нужны.
  const toggleLog = useCallback(
    (expanded: boolean) => {
      setLogOpen(expanded);
      if (!expanded || log) return;
      setLogError("");
      void api<LogPayload>(`/api/admin/requests/${requestId}/log?tail=200`)
        .then((payload) => setLog(payload.lines))
        .catch((failure: Error) => setLogError(failure.message));
    },
    [log, requestId],
  );

  const turns = usage?.turns ?? 0;
  const tokens = (usage?.input_tokens ?? 0) + (usage?.output_tokens ?? 0);

  return (
    <Section title="Отладка — видно только администратору">
      {turns ? (
        <p className="text-muted text-sm">
          Расход: {tokens.toLocaleString("ru-RU")} {plural(tokens, "токен", "токена", "токенов")} за{" "}
          {turns} {plural(turns, "прогон", "прогона", "прогонов")}
        </p>
      ) : null}

      {tests.error ? (
        <p className="text-danger text-sm">Вывод тестов недоступен: {tests.error}</p>
      ) : null}

      {tests.data?.output ? (
        <div className="flex flex-col gap-1.5">
          <h4 className="text-danger text-xs font-semibold tracking-wider uppercase">
            Вывод упавших тестов
          </h4>
          <pre className="bg-danger-soft text-danger max-h-64 overflow-auto rounded-lg p-3 font-mono text-[11px] leading-relaxed whitespace-pre-wrap">
            {tests.data.output}
          </pre>
        </div>
      ) : null}

      <Disclosure isExpanded={logOpen} onExpandedChange={toggleLog}>
        <Disclosure.Heading>
          <Button size="sm" slot="trigger" variant="tertiary">
            Сырой лог агента
            <Disclosure.Indicator />
          </Button>
        </Disclosure.Heading>
        <Disclosure.Content>
          <Disclosure.Body className="pt-2">
            {logError ? <p className="text-danger text-sm">{logError}</p> : null}
            {!log && !logError ? (
              <span className="text-muted flex items-center gap-2 text-sm">
                <Spinner className="size-4" /> Читаю лог…
              </span>
            ) : null}
            {log && log.length ? (
              <pre className="bg-surface-secondary max-h-72 overflow-auto rounded-lg p-3 font-mono text-[11px] leading-relaxed whitespace-pre-wrap">
                {log.join("\n")}
              </pre>
            ) : null}
            {log && !log.length ? <p className="text-muted text-sm">Лог пуст.</p> : null}
          </Disclosure.Body>
        </Disclosure.Content>
      </Disclosure>

      <div className="flex flex-wrap gap-2">
        {prNumber && RECHECKABLE.has(status) ? (
          <Button
            size="sm"
            variant="secondary"
            {...actions.props("recheck")}
            onPress={() =>
              actions.run("recheck", async () => {
                await post(`/api/admin/requests/${requestId}/recheck`);
                toast.success("Проверки GitHub опрошены заново");
              })
            }
          >
            Проверить заново
          </Button>
        ) : null}
        {TERMINAL.has(status) ? null : (
          <Button
            size="sm"
            variant="danger-soft"
            {...actions.props("force-cancel")}
            onPress={() => {
              if (!confirm("Снять заявку принудительно? Прогон агента будет прерван.")) return;
              actions.run("force-cancel", async () => {
                await post(`/api/admin/requests/${requestId}/force-cancel`);
                toast.success("Заявка снята");
              });
            }}
          >
            Снять принудительно
          </Button>
        )}
      </div>
    </Section>
  );
}
