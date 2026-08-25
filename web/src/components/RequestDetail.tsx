import { useEffect, useRef, useState } from "react";
import { Alert, Button, Card, Link, Separator, TextArea } from "@heroui/react";
import type { EventItem, Me, RequestItem } from "../types";
import { CANCELLABLE, STEPS, clockTime, statusOf, titleOf, when } from "../status";
import { StatusChip } from "./RequestList";
import {
  AttachButton,
  AttachmentStrip,
  AuthedImage,
  dropHandlers,
  pasteHandler,
  useAttachments,
} from "./Images";

function Steps({ request }: { request: RequestItem }) {
  const meta = statusOf(request);
  const broken = meta.step === -1;
  const current = useRef<HTMLDivElement>(null);

  // На узком экране шкала не помещается — подкручиваем к текущему шагу.
  useEffect(() => {
    current.current?.scrollIntoView({ block: "nearest", inline: "center" });
  }, [meta.step]);

  return (
    <div className="flex gap-1.5 overflow-x-auto px-5 py-4">
      {STEPS.map((label, index) => {
        const done = !broken && index < meta.step;
        const current_ = !broken && index === meta.step;
        return (
          <div
            key={label}
            ref={current_ ? current : undefined}
            className="min-w-[94px] flex-1 pr-2.5 text-[11.5px] whitespace-nowrap"
          >
            <div
              className={`mb-1.5 h-1 rounded-full ${
                broken && index === 0
                  ? "bg-danger"
                  : current_
                    ? "bg-accent"
                    : done
                      ? "bg-accent/45"
                      : "bg-surface-secondary"
              }`}
            />
            <span className={current_ ? "text-foreground font-medium" : "text-muted"}>{label}</span>
          </div>
        );
      })}
    </div>
  );
}

function Section({ title, children }: { title?: string; children: React.ReactNode }) {
  return (
    <>
      <Separator />
      <div className="flex flex-col gap-3 px-5 py-4">
        {title ? (
          <h3 className="text-muted text-xs font-semibold tracking-wider uppercase">{title}</h3>
        ) : null}
        {children}
      </div>
    </>
  );
}

function Actions({
  request,
  me,
  onAnswer,
  onApprove,
  onCancel,
  onRetry,
}: {
  request: RequestItem;
  me: Me;
  onAnswer: (text: string, images: string[]) => Promise<void>;
  onApprove: () => Promise<void>;
  onCancel: () => Promise<void>;
  onRetry: () => Promise<void>;
}) {
  const [answer, setAnswer] = useState("");
  const [pending, setPending] = useState(false);
  const attachments = useAttachments();

  const run = async (action: () => Promise<void>) => {
    setPending(true);
    try {
      await action();
    } finally {
      setPending(false);
    }
  };

  if (request.status === "needs_input") {
    return (
      <Section>
        <Alert className="bg-warning-soft" status="warning">
          <Alert.Indicator />
          <Alert.Content>
            <Alert.Title>Вопрос по заявке</Alert.Title>
            <Alert.Description>{request.question}</Alert.Description>
          </Alert.Content>
        </Alert>
        <div className="flex flex-col items-start gap-2" {...dropHandlers(attachments.add)}>
          <TextArea
            aria-label="Ваш ответ"
            className="w-full"
            placeholder="Ваш ответ — можно вставить скриншот"
            rows={3}
            value={answer}
            variant="secondary"
            onChange={(event) => setAnswer(event.target.value)}
            onPaste={pasteHandler(attachments.add)}
          />
          <AttachmentStrip items={attachments.items} onRemove={attachments.remove} />
          <div className="flex flex-wrap items-center gap-2">
            <Button
              isDisabled={!answer.trim() || attachments.uploading}
              isPending={pending}
              onPress={() =>
                run(async () => {
                  await onAnswer(answer.trim(), attachments.ids);
                  setAnswer("");
                  attachments.reset();
                })
              }
            >
              Ответить
            </Button>
            <AttachButton onPick={attachments.add} />
          </div>
        </div>
      </Section>
    );
  }

  if (request.status === "review") {
    return (
      <Section>
        <Alert className="bg-success-soft" status="success">
          <Alert.Indicator />
          <Alert.Content>
            <Alert.Title>Правка готова</Alert.Title>
            <Alert.Description>
              Автоматическая проверка прошла. После подтверждения правка появится на сайте в течение
              пары минут.
            </Alert.Description>
            <div className="mt-3 flex flex-col gap-2">
              <Button fullWidth isPending={pending} onPress={() => run(onApprove)}>
                Выкатить на сайт
              </Button>
              <p className="text-muted text-xs">
                Увидеть результат можно будет уже на самом сайте. Если что-то окажется не так —
                отправьте заявку «верни как было», это делается так же быстро.
              </p>
            </div>
          </Alert.Content>
        </Alert>
        <div>
          <Button size="sm" variant="tertiary" onPress={() => run(onCancel)}>
            Отменить заявку
          </Button>
        </div>
      </Section>
    );
  }

  if (request.status === "done") {
    return (
      <Section>
        <Alert className="bg-success-soft" status="success">
          <Alert.Indicator />
          <Alert.Content>
            <Alert.Title>Готово — правка на сайте</Alert.Title>
            {me.project.site ? (
              <Alert.Description>
                <Link href={me.project.site} rel="noopener noreferrer" target="_blank">
                  Открыть сайт
                  <Link.Icon aria-hidden="true" />
                </Link>
              </Alert.Description>
            ) : null}
          </Alert.Content>
        </Alert>
      </Section>
    );
  }

  if (["failed", "tests_failed", "no_changes"].includes(request.status)) {
    const bad = request.status !== "no_changes";
    return (
      <Section>
        <Alert className={bad ? "bg-danger-soft" : "bg-surface-secondary"} status={bad ? "danger" : "default"}>
          <Alert.Indicator />
          <Alert.Content>
            <Alert.Title>{bad ? "Не получилось" : "Менять ничего не пришлось"}</Alert.Title>
            <Alert.Description>
              {request.error || request.summary || "Заявка завершилась без изменений."}
              {request.checks_detail ? ` (${request.checks_detail})` : ""}
            </Alert.Description>
            <div className="mt-3">
              <Button isPending={pending} variant="secondary" onPress={() => run(onRetry)}>
                Отправить заново
              </Button>
            </div>
          </Alert.Content>
        </Alert>
      </Section>
    );
  }

  if (CANCELLABLE.has(request.status)) {
    return (
      <Section>
        <div>
          <Button isPending={pending} size="sm" variant="tertiary" onPress={() => run(onCancel)}>
            Отменить заявку
          </Button>
        </div>
      </Section>
    );
  }
  return null;
}

function TextChanges({ request }: { request: RequestItem }) {
  if (!request.text_changes?.length) return null;
  return (
    <Section title="Что изменилось в тексте">
      <div className="flex flex-col gap-2">
        {request.text_changes.slice(0, 12).map((change, index) => (
          <div key={index} className="border-separator overflow-hidden rounded-lg border text-sm">
            <div className="bg-surface-secondary text-muted px-3 py-1.5 text-[11.5px]">
              {change.file}
            </div>
            {change.before ? (
              <div className="text-danger px-3 py-2 break-words whitespace-pre-wrap line-through">
                {change.before}
              </div>
            ) : null}
            {change.after ? (
              <div className="text-success border-separator border-t px-3 py-2 break-words whitespace-pre-wrap">
                {change.after}
              </div>
            ) : null}
          </div>
        ))}
      </div>
      {request.files?.length ? (
        <details className="text-muted text-sm">
          <summary className="cursor-pointer">Затронуто файлов: {request.files.length}</summary>
          <ul className="mt-2 list-inside list-disc font-mono text-xs">
            {request.files.map((file) => (
              <li key={file}>{file}</li>
            ))}
          </ul>
        </details>
      ) : null}
    </Section>
  );
}

function Feed({ events }: { events: EventItem[] }) {
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [events.length]);

  if (!events.length) return null;
  return (
    <Section title="Ход работы">
      <div ref={box} className="flex max-h-80 flex-col gap-2 overflow-y-auto">
        {events.slice(-80).map((event, index) => (
          <div key={index} className="flex gap-2.5 text-sm leading-snug">
            <span className="text-muted flex-none pt-0.5 text-xs tabular-nums">
              {clockTime(event.ts)}
            </span>
            <span
              className={
                event.kind === "agent"
                  ? "bg-surface-secondary rounded-lg px-3 py-2 whitespace-pre-wrap"
                  : event.kind === "user"
                    ? "bg-accent-soft rounded-lg px-3 py-2 whitespace-pre-wrap"
                    : event.kind === "error"
                      ? "text-danger whitespace-pre-wrap"
                      : "text-muted whitespace-pre-wrap"
              }
            >
              {event.text}
            </span>
          </div>
        ))}
      </div>
    </Section>
  );
}

export function RequestDetail(props: {
  request: RequestItem;
  events: EventItem[];
  me: Me;
  onBack: () => void;
  onAnswer: (text: string, images: string[]) => Promise<void>;
  onApprove: () => Promise<void>;
  onCancel: () => Promise<void>;
  onRetry: () => Promise<void>;
}) {
  const { request, events, me, onBack } = props;
  const isAdmin = me.role === "admin";

  return (
    <Card className="overflow-hidden p-0">
      <div className="flex flex-col gap-2 px-5 pt-5 pb-4">
        <div className="lg:hidden">
          <Button size="sm" variant="ghost" onPress={onBack}>
            ← К списку
          </Button>
        </div>
        <h2 className="text-lg leading-tight font-semibold">{titleOf(request)}</h2>
        <div className="text-muted flex flex-wrap items-center gap-2.5 text-xs">
          <StatusChip request={request} />
          <span>Заявка №{request.id}</span>
          <span>{when(request.created_at)}</span>
          {isAdmin && request.author ? <span>{request.author}</span> : null}
          {isAdmin && request.pr_url ? (
            <Link className="text-xs" href={request.pr_url} rel="noopener noreferrer" target="_blank">
              PR на GitHub
            </Link>
          ) : null}
        </div>
      </div>

      <Separator />
      <Steps request={request} />

      <Section title="Просьба">
        <div className="bg-surface-secondary rounded-lg px-3.5 py-3 whitespace-pre-wrap">
          {request.body}
        </div>
        {request.images?.length ? (
          <div className="flex flex-wrap gap-2">
            {request.images.map((name) => (
              <AuthedImage
                key={name}
                alt="Картинка к заявке"
                path={`/api/requests/${request.id}/images/${name}`}
              />
            ))}
          </div>
        ) : null}
      </Section>

      <Actions {...props} />

      {request.summary || request.user_visible?.length ? (
        <Section title="Что сделано">
          {request.summary ? <p className="leading-relaxed">{request.summary}</p> : null}
          {request.user_visible?.length ? (
            <ul className="list-inside list-disc space-y-1">
              {request.user_visible.map((item, index) => (
                <li key={index}>{item}</li>
              ))}
            </ul>
          ) : null}
          {request.notes ? <p className="text-muted text-sm">Важно: {request.notes}</p> : null}
        </Section>
      ) : null}

      <TextChanges request={request} />
      <Feed events={events} />
    </Card>
  );
}
