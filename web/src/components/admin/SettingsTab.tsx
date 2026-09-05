import { useState } from "react";
import type { ReactNode } from "react";
import {
  Alert,
  Button,
  Chip,
  Description,
  FieldError,
  Input,
  Label,
  Switch,
  TextField,
} from "@heroui/react";
import { api } from "../../api";
import { loadMeta } from "../../status";
import type { GithubTokenSource, GithubTokenState, MetaPayload, StatusTone } from "../../types";
import { LoadError, Loading, formatMoment, post, useActions, useAdminData } from "./shared";

const APPROVAL: Record<string, string> = {
  user: "Подтверждает автор заявки",
  admin: "Подтверждает только администратор",
};

/** Значение бывает и чипом, поэтому не строка: подписи слева должны совпадать. */
function Row({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="grid grid-cols-[170px_1fr] gap-x-3 gap-y-1 text-sm">
      <dt className="text-muted">{label}</dt>
      <dd className="break-words">{value}</dd>
    </div>
  );
}

/** Состояние словами: администратору важно, откуда взят ключ, а не флаг в JSON. */
const SOURCE: Record<GithubTokenSource, { label: string; tone: StatusTone }> = {
  ui: { label: "настроен через интерфейс", tone: "success" },
  env: { label: "взят из переменной окружения", tone: "accent" },
  none: { label: "не задан", tone: "danger" },
};

const MIN_LENGTH = 20;
const MAX_LENGTH = 255;

/**
 * Те же рамки, что и на сервере. Смысл дублирования — не безопасность, а
 * скорость ответа: за опечатку не должен платить запрос в GitHub.
 *
 * Края обрезаем первыми — ровно как validated_token на сервере. При выделении
 * PAT мышью в буфер уезжает хвостовой перенос строки, в поле типа password его
 * не видно, и человек упирался на единственном шаге, ради которого форма и
 * делалась: сервер такой ключ принимал, а кнопка оставалась заблокированной.
 * Пробел ВНУТРИ остаётся отказом — там скопирован не ключ, а строка вокруг него.
 */
function tokenProblem(raw: string): string {
  const value = raw.trim();
  if (value === "") return "";
  if (/\s/.test(value)) {
    return "В ключе есть пробел или перенос строки — похоже, скопировалось лишнее.";
  }
  // Кириллица и управляющие символы не переживут заголовок Authorization, так
  // что до GitHub такой ключ всё равно не доедет — отказываем сразу.
  if (!/^[\x21-\x7e]+$/.test(value)) {
    return "В ключе есть посторонние символы — в токене GitHub только латиница и цифры.";
  }
  if (value.length < MIN_LENGTH || value.length > MAX_LENGTH) {
    return `Ключ должен быть длиной от ${MIN_LENGTH} до ${MAX_LENGTH} символов, а в этом — ${value.length}.`;
  }
  return "";
}

/**
 * Форма ключа. Ошибку показываем текстом рядом с полем, а не всплывашкой:
 * сервер объясняет отказ человеческими словами («токен не видит репозиторий»),
 * и это объяснение нужно читать вместе с тем, что вставлено в поле.
 */
function GithubToken({
  state,
  onChange,
}: {
  state: GithubTokenState;
  onChange: (next: GithubTokenState) => void;
}) {
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState("");
  const [failure, setFailure] = useState("");
  const [done, setDone] = useState("");

  // Отправляем на сервер то же значение, которое проверяли: он тоже делает strip.
  const trimmed = token.trim();
  const problem = tokenProblem(token);
  const ready = trimmed !== "" && problem === "";
  const source = SOURCE[state.source];
  // Две разные беды, и путать их нельзя: «проверять не на чем» — это дырка в
  // настройках инстанса, из-за которой ни один ключ не сохранится, и сказать
  // о ней нужно раньше, чем человек начнёт вставлять токен за токеном.
  const blocked = Boolean(state.error);
  const powerless = !blocked && state.configured && state.can_push === false;

  const save = (): void => {
    if (!ready || busy !== "") return;
    setBusy("save");
    setFailure("");
    setDone("");
    void api<GithubTokenState>("/api/admin/github-token", {
      method: "PUT",
      body: JSON.stringify({ token: trimmed }),
    })
      .then((next) => {
        onChange(next);
        // Поле чистим только после успеха: на отказе человек правит вставленное,
        // а не ищет ключ заново.
        setToken("");
        // Словами ровно про то, что проверено: запись в файлы — по правам
        // репозитория, pull requests — отдельным запросом на чтение списка.
        setDone("Токен принят: репозиторий виден, запись в файлы разрешена, pull requests доступны.");
      })
      .catch((failed: Error) => setFailure(failed.message))
      .finally(() => setBusy(""));
  };

  const drop = (): void => {
    if (busy !== "") return;
    if (
      !confirm(
        "Удалить токен, вписанный через интерфейс? Шлюз вернётся к переменной окружения — если её нет, пуш перестанет работать.",
      )
    ) {
      return;
    }
    setBusy("drop");
    setFailure("");
    setDone("");
    // Ответ на удаление не разбираем, а спрашиваем состояние заново: только так
    // видно, осталась ли под интерфейсным ключом переменная окружения.
    void api<unknown>("/api/admin/github-token", { method: "DELETE" })
      .then(() => api<GithubTokenState>("/api/admin/github-token"))
      .then((next) => {
        onChange(next);
        setDone(
          next.configured
            ? "Ключ из интерфейса убран — шлюз вернулся к переменной окружения."
            : "Токен удалён. Пока не вписан новый, заявки будут срываться на пуше.",
        );
      })
      .catch((failed: Error) => setFailure(failed.message))
      .finally(() => setBusy(""));
  };

  return (
    <>
      <dl className="flex flex-col gap-1.5">
        <Row
          label="Состояние"
          value={
            <Chip color={source.tone} size="sm" variant="soft">
              <Chip.Label>{source.label}</Chip.Label>
            </Chip>
          }
        />
        {state.configured ? <Row label="Ключ" value={state.hint || "—"} /> : null}
        {state.configured ? <Row label="Проверен" value={formatMoment(state.checked_at)} /> : null}
        <Row label="Репозиторий" value={state.repo || "—"} />
      </dl>

      {blocked ? (
        <Alert className="bg-danger-soft" status="danger">
          <Alert.Indicator />
          <Alert.Content>
            <Alert.Title>Ключ проверить не на чем</Alert.Title>
            <Alert.Description>{state.error}</Alert.Description>
          </Alert.Content>
        </Alert>
      ) : null}

      {powerless ? (
        <Alert className="bg-danger-soft" status="danger">
          <Alert.Indicator />
          <Alert.Content>
            <Alert.Title>Токен не проходит проверку</Alert.Title>
            <Alert.Description>
              Токен видит репозиторий, но не может в него писать.
            </Alert.Description>
          </Alert.Content>
        </Alert>
      ) : null}

      <form
        className="flex flex-col gap-3"
        onSubmit={(event) => {
          event.preventDefault();
          save();
        }}
      >
        <TextField
          className="max-w-[420px]"
          isInvalid={problem !== ""}
          type="password"
          value={token}
          variant="secondary"
          onChange={setToken}
        >
          <Label>{state.configured ? "Новый токен" : "Токен"}</Label>
          <Input autoComplete="off" placeholder="github_pat_…" />
          {problem ? (
            <FieldError>{problem}</FieldError>
          ) : (
            <Description>
              Сохраняем только после того, как сходим с ним в GitHub и убедимся, что пуш пройдёт.
            </Description>
          )}
        </TextField>

        <div className="flex flex-wrap items-center gap-2">
          <Button isDisabled={!ready || busy !== ""} isPending={busy === "save"} type="submit">
            Проверить и сохранить
          </Button>
          {state.configured ? (
            <Button
              isDisabled={busy !== ""}
              isPending={busy === "drop"}
              variant="danger-soft"
              onPress={drop}
            >
              Удалить
            </Button>
          ) : null}
        </div>
      </form>

      {failure ? <p className="text-danger text-sm">{failure}</p> : null}
      {done ? <p className="text-success text-sm">{done}</p> : null}

      <p className="text-muted text-xs">
        Нужен fine-grained personal access token, выданный на {state.repo || "рабочий репозиторий"}:
        права Contents — Read and write и Pull requests — Read and write. Токен остаётся в базе
        шлюза и обратно в интерфейс не приходит — здесь виден только его хвост.
      </p>
    </>
  );
}

/**
 * Секция ключа тянет своё состояние сама: без токена шлюз не может запушить
 * ни одну заявку, поэтому чинить его нужно и тогда, когда остальная админка
 * не отвечает.
 */
function GithubAccess() {
  const state = useAdminData<GithubTokenState>("/api/admin/github-token");

  return (
    <section className="border-separator flex flex-col gap-3 rounded-xl border p-4">
      <h3 className="text-muted text-xs font-semibold tracking-wider uppercase">Доступ к GitHub</h3>
      <p className="text-muted text-sm">
        Этим ключом шлюз отправляет ветку и открывает pull request. Вписанный здесь важнее
        переменной окружения и подхватывается сразу, без перезапуска.
      </p>
      {state.error ? <LoadError message={state.error} onRetry={state.reload} /> : null}
      {!state.error && !state.data ? <Loading /> : null}
      {state.data ? (
        <GithubToken state={state.data} onChange={(next) => state.setData(next)} />
      ) : null}
    </section>
  );
}

/**
 * Единственный переключатель, который можно щёлкнуть на ходу, — пауза приёма.
 * Остальное приходит из переменных окружения при запуске, поэтому показываем
 * только текущие значения: править их отсюда было бы враньём.
 */
function InstanceSettings({
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
  return (
    <div className="flex flex-col gap-6">
      <InstanceSettings
        error={error}
        meta={meta}
        onPausedChange={onPausedChange}
        onRetry={onRetry}
      />
      <GithubAccess />
    </div>
  );
}
