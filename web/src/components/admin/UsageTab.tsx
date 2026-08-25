import { Table } from "@heroui/react";
import type { UsageResponse, UsageRow } from "../../types";
import { LoadError, Loading, formatNumber, plural, useAdminData } from "./shared";

/** Расход модели по людям: видно, кто и сколько тратит и куда уходит счёт. */
export function UsageTab() {
  const state = useAdminData<UsageResponse>("/api/admin/usage?days=30");

  if (state.error) return <LoadError message={state.error} onRetry={state.reload} />;
  if (!state.data) return <Loading />;

  const { days, totals } = state.data;
  const period = `${days} ${plural(days, "день", "дня", "дней")}`;

  if (!totals.length) {
    return (
      <p className="text-muted py-8 text-center text-sm">
        За последние {period} заявок не было — считать нечего.
      </p>
    );
  }

  const sum = (pick: (row: UsageRow) => number): number =>
    totals.reduce((acc, row) => acc + pick(row), 0);
  const rows = totals.map((row) => ({ ...row, id: row.user }));

  return (
    <div className="flex flex-col gap-3">
      <p className="text-muted text-sm">За последние {period}.</p>
      <Table>
        <Table.ScrollContainer>
          <Table.Content aria-label="Расход по людям" className="min-w-[460px]">
            <Table.Header>
              <Table.Column isRowHeader id="user">
                Человек
              </Table.Column>
              <Table.Column id="requests">Заявок</Table.Column>
              <Table.Column id="input">Токенов на вход</Table.Column>
              <Table.Column id="output">Токенов на выход</Table.Column>
            </Table.Header>
            <Table.Body items={rows}>
              {(row) => (
                <Table.Row>
                  <Table.Cell>{row.author || row.user}</Table.Cell>
                  <Table.Cell className="tabular-nums">{formatNumber(row.requests)}</Table.Cell>
                  <Table.Cell className="tabular-nums">{formatNumber(row.input_tokens)}</Table.Cell>
                  <Table.Cell className="tabular-nums">{formatNumber(row.output_tokens)}</Table.Cell>
                </Table.Row>
              )}
            </Table.Body>
          </Table.Content>
        </Table.ScrollContainer>
        <Table.Footer className="text-muted flex flex-wrap gap-x-4 gap-y-1 px-4 py-3 text-sm">
          <span>
            Всего заявок: <span className="tabular-nums">{formatNumber(sum((row) => row.requests))}</span>
          </span>
          <span>
            Вход: <span className="tabular-nums">{formatNumber(sum((row) => row.input_tokens))}</span>
          </span>
          <span>
            Выход: <span className="tabular-nums">{formatNumber(sum((row) => row.output_tokens))}</span>
          </span>
        </Table.Footer>
      </Table>
    </div>
  );
}
