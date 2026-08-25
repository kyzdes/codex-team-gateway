import { Alert, Switch } from "@heroui/react";
import { loadMeta } from "../../status";
import type { MetaPayload } from "../../types";
import { LoadError, Loading, post, useActions } from "./shared";

const APPROVAL: Record<string, string> = {
  user: "Подтверждает автор заявки",
  admin: "Подтверждает только администратор",
};

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="grid grid-cols-[170px_1fr] gap-x-3 gap-y-1 text-sm">
      <dt className="text-muted">{label}</dt>
      <dd className="break-words">{value}</dd>
    </div>
  );
}

/**
 * Единственный переключатель, который можно щёлкнуть на ходу, — пауза приёма.
 * Остальное приходит из переменных окружения при запуске, поэтому показываем
 * только текущие значения: править их отсюда было бы враньём.
 */
export function SettingsTab({
  error,
  meta,
  onPausedChange,
  onRetry,
}: {
  error: string;
  meta: MetaPayload | null;
  onPausedChange: (paused: boolean) => void;
  onRetry: () => void;
}) {
  const actions = useActions();

  if (error) return <LoadError message={error} onRetry={onRetry} />;
  if (!meta) return <Loading />;

  const limit = meta.limits.rate_limit_per_hour;

  return (
    <div className="flex flex-col gap-5">
      <div className="border-separator flex flex-col gap-2.5 rounded-xl border p-4">
        <Switch
          isDisabled={actions.pending !== ""}
          isSelected={meta.paused}
          onChange={(paused) =>
            actions.run("pause", async () => {
              const result = await post<{ paused: boolean }>("/api/admin/pause", { paused });
              onPausedChange(result.paused);
              // Подписи и флаги инстанса кэшируются модулем status: обновляем их,
              // чтобы остальной интерфейс узнал про паузу без перезагрузки.
              await loadMeta();
            })
          }
        >
          <Switch.Content>
            <Switch.Control>
              <Switch.Thumb />
            </Switch.Control>
            Приостановить приём заявок
          </Switch.Content>
        </Switch>
        <p className="text-muted text-sm">
          Заявки продолжат приниматься, но останутся в очереди — агента не запускаем. Когда снимете
          паузу, всё накопившееся уйдёт в работу.
        </p>
      </div>

      {meta.paused ? (
        <Alert className="bg-warning-soft" status="warning">
          <Alert.Indicator />
          <Alert.Content>
            <Alert.Title>Приём приостановлен</Alert.Title>
            <Alert.Description>
              Новые заявки копятся в очереди и ничего не меняют на сайте.
            </Alert.Description>
          </Alert.Content>
        </Alert>
      ) : null}

      <dl className="flex flex-col gap-1.5">
        <Row
          label="Подтверждение выкатки"
          value={APPROVAL[meta.approval_policy] ?? meta.approval_policy}
        />
        <Row
          label="Заявок в час"
          value={limit > 0 ? `не больше ${limit} на человека` : "без ограничения"}
        />
        <Row label="Картинок к сообщению" value={String(meta.limits.max_images)} />
      </dl>

      <p className="text-muted text-xs">
        Эти три значения задаются переменными окружения при запуске шлюза — здесь они только для
        сверки.
      </p>
    </div>
  );
}
