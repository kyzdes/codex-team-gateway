import { Alert, Button, Chip } from "@heroui/react";
import type { ReadinessResponse } from "../../types";
import { LoadError, Loading, plural, useAdminData } from "./shared";

/**
 * Чек-лист готовности инстанса. Смысл вкладки — увидеть нехватку до первой
 * заявки: без логина Codex, права записи в репозиторий или AGENTS.md падает
 * каждая заявка, а сотрудник видит только безликое «Ошибка».
 */
export function ReadinessTab() {
  const state = useAdminData<ReadinessResponse>("/api/admin/readiness");

  if (state.error) return <LoadError message={state.error} onRetry={state.reload} />;
  if (!state.data) return <Loading />;

  const { checks, problems } = state.data;
  const broken = checks.filter((check) => !check.ok);

  return (
    <div className="flex flex-col gap-4">
      {problems.length ? (
        <Alert className="bg-warning-soft" status="warning">
          <Alert.Indicator />
          <Alert.Content>
            <Alert.Title>Проверьте переменные окружения</Alert.Title>
            <Alert.Description>
              {/* Опечатку в переменной не поймает ни один пункт списка ниже:
                  там смотрят наружу, а это про то, с чем запустился сам шлюз. */}
              <ul className="list-inside list-disc space-y-1">
                {problems.map((problem) => (
                  <li key={problem}>{problem}</li>
                ))}
              </ul>
            </Alert.Description>
          </Alert.Content>
        </Alert>
      ) : null}

      {broken.length ? (
        <Alert className="bg-danger-soft" status="danger">
          <Alert.Indicator />
          <Alert.Content>
            <Alert.Title>Шлюз не готов к работе</Alert.Title>
            <Alert.Description>
              {broken.length} {plural(broken.length, "пункт", "пункта", "пунктов")} ниже{" "}
              {plural(broken.length, "требует", "требуют", "требуют")} внимания — пока их не
              поправить, заявки будут срываться.
            </Alert.Description>
          </Alert.Content>
        </Alert>
      ) : (
        <Alert className="bg-success-soft" status="success">
          <Alert.Indicator />
          <Alert.Content>
            <Alert.Title>Всё на месте</Alert.Title>
            <Alert.Description>Проверки пройдены, заявки можно принимать.</Alert.Description>
          </Alert.Content>
        </Alert>
      )}

      <div className="border-separator divide-separator flex flex-col divide-y rounded-xl border">
        {checks.map((check) => (
          <div key={check.key} className="flex flex-col gap-1.5 px-4 py-3">
            <div className="flex items-start justify-between gap-3">
              <span className="leading-snug font-medium">{check.title}</span>
              <Chip color={check.ok ? "success" : "danger"} size="sm" variant="soft">
                <Chip.Label>{check.ok ? "готово" : "не готово"}</Chip.Label>
              </Chip>
            </div>
            {check.detail ? (
              <p className="text-muted text-sm break-words">{check.detail}</p>
            ) : null}
            {!check.ok && check.hint ? <p className="text-sm">Что сделать: {check.hint}</p> : null}
          </div>
        ))}
      </div>

      <div>
        <Button size="sm" variant="secondary" onPress={state.reload}>
          Проверить заново
        </Button>
      </div>
    </div>
  );
}
