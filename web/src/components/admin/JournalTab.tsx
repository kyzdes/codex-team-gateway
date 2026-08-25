import { Chip, Link, Table } from "@heroui/react";
import type { JournalResponse, StatusMetaPayload } from "../../types";
import { LoadError, Loading, formatMoment, useAdminData } from "./shared";

/**
 * Кто и когда нажал «Выкатить» — единственный след ответственности за правку
 * на сайте. Подписи статусов берём из /api/meta, чтобы они не разъезжались с
 * теми, что видит автор заявки.
 */
export function JournalTab({ statuses }: { statuses: Record<string, StatusMetaPayload> }) {
  const state = useAdminData<JournalResponse>("/api/admin/journal?limit=50");

  if (state.error) return <LoadError message={state.error} onRetry={state.reload} />;
  if (!state.data) return <Loading />;

  const entries = state.data.entries;
  if (!entries.length) {
    return <p className="text-muted py-8 text-center text-sm">Выкаток пока не было.</p>;
  }

  return (
    <Table>
      <Table.ScrollContainer>
        <Table.Content aria-label="Журнал подтверждений" className="min-w-[560px]">
          <Table.Header>
            <Table.Column isRowHeader id="request">
              Заявка
            </Table.Column>
            <Table.Column id="approved_by">Подтвердил</Table.Column>
            <Table.Column id="approved_at">Когда</Table.Column>
            <Table.Column id="status">Чем кончилось</Table.Column>
          </Table.Header>
          <Table.Body items={entries}>
            {(entry) => {
              const meta = statuses[entry.status];
              return (
                <Table.Row>
                  <Table.Cell>
                    <span className="flex flex-col gap-1">
                      <span className="font-medium">{entry.title || `Заявка №${entry.id}`}</span>
                      <span className="text-muted text-xs">
                        №{entry.id} · автор {entry.author || entry.user}
                      </span>
                    </span>
                  </Table.Cell>
                  <Table.Cell>{entry.approver || entry.approved_by || "—"}</Table.Cell>
                  <Table.Cell className="whitespace-nowrap">
                    {formatMoment(entry.approved_at)}
                  </Table.Cell>
                  <Table.Cell>
                    <span className="flex flex-wrap items-center gap-2">
                      <Chip color={meta?.tone ?? "default"} size="sm" variant="soft">
                        <Chip.Label>{meta?.label ?? entry.status}</Chip.Label>
                      </Chip>
                      {entry.pr_url ? (
                        <Link
                          className="text-xs"
                          href={entry.pr_url}
                          rel="noopener noreferrer"
                          target="_blank"
                        >
                          PR
                        </Link>
                      ) : null}
                    </span>
                  </Table.Cell>
                </Table.Row>
              );
            }}
          </Table.Body>
        </Table.Content>
      </Table.ScrollContainer>
    </Table>
  );
}
