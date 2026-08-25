import { Button, Card, Link, Separator } from "@heroui/react";
import type { EventItem } from "../types";
import { titleOf, when } from "../status";
import { StatusChip } from "./RequestList";
import { AuthedImage } from "./Images";
import { Section, Steps } from "./Steps";
import { RequestActions, type RequestActionsProps } from "./RequestActions";
import { RequestChanges } from "./RequestChanges";
import { RequestFeed } from "./RequestFeed";
import { RequestDebug } from "./admin/RequestDebug";

export interface RequestDetailProps extends RequestActionsProps {
  events: EventItem[];
  onBack: () => void;
}

/**
 * Каркас карточки заявки: шапка, шкала шагов и порядок секций. Сами секции
 * решают, показываться им или нет, — поэтому здесь нет ни одного условия.
 */
export function RequestDetail(props: RequestDetailProps) {
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
            <Link
              className="text-xs"
              href={request.pr_url}
              rel="noopener noreferrer"
              target="_blank"
            >
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

      <RequestActions {...props} />

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

      <RequestChanges request={request} />
      <RequestFeed events={events} />
      {isAdmin ? (
        <RequestDebug
          prNumber={request.pr_number}
          requestId={request.id}
          status={request.status}
          usage={request.usage}
        />
      ) : null}
    </Card>
  );
}
