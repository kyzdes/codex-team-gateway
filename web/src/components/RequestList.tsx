import { Card, Chip, Spinner } from "@heroui/react";
import type { RequestItem } from "../types";
import { statusOf, titleOf, when } from "../status";

export function StatusChip({ request, size = "sm" }: { request: RequestItem; size?: "sm" | "md" }) {
  const meta = statusOf(request);
  return (
    <Chip color={meta.color} size={size} variant="soft">
      {meta.busy ? <Spinner className="size-3" /> : null}
      <Chip.Label>{meta.label}</Chip.Label>
    </Chip>
  );
}

export function RequestList({
  requests,
  selectedId,
  showAuthor,
  onSelect,
}: {
  requests: RequestItem[];
  selectedId: number | null;
  showAuthor: boolean;
  onSelect: (id: number) => void;
}) {
  if (!requests.length) {
    return (
      <p className="text-muted px-4 py-6 text-center text-sm">
        Пока ни одной заявки. Опишите первую — это займёт минуту.
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-2">
      {requests.map((request) => (
        <Card<"button">
          key={request.id}
          aria-current={request.id === selectedId}
          className={`cursor-pointer text-left transition-colors ${
            request.id === selectedId ? "ring-accent ring-2" : "hover:bg-surface-hover"
          }`}
          render={(props) => (
            <button {...props} type="button" onClick={() => onSelect(request.id)} />
          )}
        >
          <Card.Content className="flex flex-col gap-2">
            <span className="leading-snug font-medium">{titleOf(request)}</span>
            <span className="flex items-center justify-between gap-2">
              <StatusChip request={request} />
              <span className="text-muted text-xs">
                {showAuthor && request.author ? `${request.author} · ` : ""}
                {when(request.created_at)}
              </span>
            </span>
          </Card.Content>
        </Card>
      ))}
    </div>
  );
}
