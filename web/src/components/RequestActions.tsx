import { useState } from "react";
import { Alert, Button, Link, TextArea } from "@heroui/react";
import type { Me, RequestItem } from "../types";
import { CANCELLABLE, approvalPolicy } from "../status";
import { Section } from "./Steps";
import {
  AttachButton,
  AttachmentStrip,
  dropHandlers,
  pasteHandler,
  useAttachments,
} from "./Images";

export interface RequestActionsProps {
  request: RequestItem;
  me: Me;
  onAnswer: (text: string, images: string[]) => Promise<void>;
  onApprove: () => Promise<void>;
  onCancel: () => Promise<void>;
  onRetry: () => Promise<void>;
}

/** Пока запрос летит, кнопку надо держать занятой — иначе человек жмёт дважды. */
function usePending(): [boolean, (action: () => Promise<void>) => Promise<void>] {
  const [pending, setPending] = useState(false);
  const run = async (action: () => Promise<void>) => {
    setPending(true);
    try {
      await action();
    } catch {
      // Текст ошибки человеку уже показали. Гасим здесь, чтобы отказ сервера
      // не считался успехом и не стирал набранный ответ.
    } finally {
      setPending(false);
    }
  };
  return [pending, run];
}

/**
 * Единственное действие, которое сейчас ждут от человека. Показываем ровно одно:
 * лишние кнопки на этом экране читаются как «а вдруг я сломаю сайт».
 */
export function RequestActions({
  request,
  me,
  onAnswer,
  onApprove,
  onCancel,
  onRetry,
}: RequestActionsProps) {
  const [answer, setAnswer] = useState("");
  const [pending, run] = usePending();
  const attachments = useAttachments();

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
            maxLength={4000}
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
    // При политике "admin" обычный человек получит 403 — лучше сказать заранее.
    const mayApprove = approvalPolicy() !== "admin" || me.role === "admin";
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
              {mayApprove ? (
                <>
                  <Button fullWidth isPending={pending} onPress={() => run(onApprove)}>
                    Выкатить на сайт
                  </Button>
                  <p className="text-muted text-xs">
                    Увидеть результат можно будет уже на самом сайте. Если что-то окажется не так —
                    отправьте заявку «верни как было», это делается так же быстро.
                  </p>
                </>
              ) : (
                <p className="text-muted text-xs">
                  Выкатку подтверждает администратор — заявка уже у него.
                </p>
              )}
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
        <Alert
          className={bad ? "bg-danger-soft" : "bg-surface-secondary"}
          status={bad ? "danger" : "default"}
        >
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
