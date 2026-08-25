import { useState } from "react";
import { Button, Card, TextArea } from "@heroui/react";
import { AttachButton, AttachmentStrip, dropHandlers, pasteHandler, useAttachments } from "./Images";

export function Composer({
  onSubmit,
}: {
  onSubmit: (text: string, images: string[]) => Promise<void>;
}) {
  const [text, setText] = useState("");
  const [pending, setPending] = useState(false);
  const attachments = useAttachments();

  const send = async () => {
    const value = text.trim();
    if (value.length < 5) return;
    setPending(true);
    try {
      await onSubmit(value, attachments.ids);
      // Поля чистим только после успеха: на отказе по лимиту или обрыве связи
      // человек не должен набирать длинную просьбу заново.
      setText("");
      attachments.reset();
    } catch {
      /* об ошибке уже сказал вызывающий */
    } finally {
      setPending(false);
    }
  };

  return (
    <Card {...dropHandlers(attachments.add)}>
      <Card.Header>
        <Card.Title className="text-base">Что поправить на сайте?</Card.Title>
      </Card.Header>
      <Card.Content className="flex flex-col gap-3">
        <TextArea
          aria-label="Текст заявки"
          className="min-h-24 w-full"
          maxLength={8000}
          placeholder="Например: на странице «Доставка» замените телефон на +7 999 123-45-67"
          value={text}
          variant="secondary"
          onChange={(event) => setText(event.target.value)}
          onPaste={pasteHandler(attachments.add)}
        />
        <AttachmentStrip items={attachments.items} onRemove={attachments.remove} />
        <AttachButton hint={!attachments.items.length} onPick={attachments.add} />
      </Card.Content>
      <Card.Footer className="flex items-center gap-3">
        <p className="text-muted flex-1 text-xs">
          Опишите словами, как объяснили бы коллеге. Если что-то не получается объяснить — покажите
          скриншотом.
        </p>
        <Button
          isDisabled={text.trim().length < 5 || attachments.uploading}
          isPending={pending}
          onPress={send}
        >
          Отправить
        </Button>
      </Card.Footer>
    </Card>
  );
}
