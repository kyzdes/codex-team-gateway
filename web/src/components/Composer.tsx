import { useState } from "react";
import { Button, Card, TextArea } from "@heroui/react";

export function Composer({ onSubmit }: { onSubmit: (text: string) => Promise<void> }) {
  const [text, setText] = useState("");
  const [pending, setPending] = useState(false);

  const send = async () => {
    const value = text.trim();
    if (value.length < 5) return;
    setPending(true);
    try {
      await onSubmit(value);
      setText("");
    } finally {
      setPending(false);
    }
  };

  return (
    <Card>
      <Card.Header>
        <Card.Title className="text-base">Что поправить на сайте?</Card.Title>
      </Card.Header>
      <Card.Content>
        <TextArea
          aria-label="Текст заявки"
          className="min-h-24 w-full"
          placeholder="Например: на странице «Доставка» замените телефон на +7 999 123-45-67"
          value={text}
          variant="secondary"
          onChange={(event) => setText(event.target.value)}
        />
      </Card.Content>
      <Card.Footer className="flex items-center gap-3">
        <p className="text-muted flex-1 text-xs">
          Опишите словами, как объяснили бы коллеге. Если что-то будет непонятно — переспросим.
        </p>
        <Button isDisabled={text.trim().length < 5} isPending={pending} onPress={send}>
          Отправить
        </Button>
      </Card.Footer>
    </Card>
  );
}
