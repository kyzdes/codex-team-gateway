import { useState } from "react";
import { Button, Chip, Description, Input, Label, Table, TextField, toast } from "@heroui/react";
import type { PeopleResponse, Person } from "../../types";
import { CopyButton, LoadError, Loading, post, useActions, useAdminData } from "./shared";

/** Логин уходит в персональную ссылку и в имя ветки — только латиница. */
const LOGIN = /^[a-z0-9_-]{2,32}$/;

function AddPerson({ onAdded }: { onAdded: (person: Person) => void }) {
  const [login, setLogin] = useState("");
  const [name, setName] = useState("");
  const actions = useActions();
  const ready = LOGIN.test(login) && name.trim() !== "";

  return (
    <form
      className="border-separator flex flex-col gap-3 rounded-xl border border-dashed p-4"
      onSubmit={(event) => {
        event.preventDefault();
        if (!ready || actions.pending !== "") return;
        actions.run("add", async () => {
          const person = await post<Person>("/api/admin/people", {
            login,
            display_name: name.trim(),
          });
          onAdded(person);
          setLogin("");
          setName("");
          toast.success(`${person.display_name} добавлен — осталось отдать ему ссылку`);
        });
      }}
    >
      <h3 className="text-muted text-xs font-semibold tracking-wider uppercase">
        Добавить человека
      </h3>
      <div className="flex flex-wrap gap-3">
        <TextField
          className="min-w-[160px] flex-1"
          isInvalid={login !== "" && !LOGIN.test(login)}
          value={login}
          variant="secondary"
          onChange={setLogin}
        >
          <Label>Логин</Label>
          <Input placeholder="masha" />
          <Description>Латиница, цифры, дефис — от 2 до 32 символов</Description>
        </TextField>
        <TextField
          className="min-w-[160px] flex-1"
          value={name}
          variant="secondary"
          onChange={setName}
        >
          <Label>Имя</Label>
          <Input placeholder="Маша" />
          <Description>Так человека увидят в заявках</Description>
        </TextField>
      </div>
      <div>
        <Button isDisabled={!ready} isPending={actions.pending === "add"} type="submit">
          Добавить
        </Button>
      </div>
    </form>
  );
}

/**
 * Люди и их персональные ссылки. Ссылка — это и есть доступ, поэтому рядом с
 * каждой строкой лежат оба рычага: отключить человека и перевыпустить ссылку,
 * если старая утекла.
 */
export function PeopleTab() {
  const state = useAdminData<PeopleResponse>("/api/admin/people");
  const actions = useActions();

  if (state.error) return <LoadError message={state.error} onRetry={state.reload} />;
  if (!state.data) return <Loading />;

  const patch = (login: string, changes: Partial<Person>): void =>
    state.setData((current) =>
      current
        ? {
            people: current.people.map((item) =>
              item.login === login ? { ...item, ...changes } : item,
            ),
          }
        : current,
    );

  const rows = state.data.people.map((person) => ({ ...person, id: person.login }));

  return (
    <div className="flex flex-col gap-4">
      {rows.length ? (
        <Table>
          <Table.ScrollContainer>
            <Table.Content aria-label="Люди и доступы" className="min-w-[620px]">
              <Table.Header>
                <Table.Column isRowHeader id="person">
                  Человек
                </Table.Column>
                <Table.Column id="link">Персональная ссылка</Table.Column>
                <Table.Column id="actions">Доступ</Table.Column>
              </Table.Header>
              <Table.Body items={rows}>
                {(person) => (
                  <Table.Row className={person.disabled ? "opacity-55" : undefined}>
                    <Table.Cell>
                      <span className="flex flex-col gap-1">
                        <span className="flex flex-wrap items-center gap-1.5">
                          <span className="font-medium">{person.display_name}</span>
                          {person.role === "admin" ? (
                            <Chip color="accent" size="sm" variant="soft">
                              <Chip.Label>админ</Chip.Label>
                            </Chip>
                          ) : null}
                          {person.disabled ? (
                            <Chip color="default" size="sm" variant="soft">
                              <Chip.Label>отключён</Chip.Label>
                            </Chip>
                          ) : null}
                        </span>
                        <span className="text-muted text-xs">{person.login}</span>
                      </span>
                    </Table.Cell>
                    <Table.Cell>
                      <span className="flex items-center gap-2">
                        <code className="text-muted max-w-[220px] truncate text-xs">
                          {person.link}
                        </code>
                        <CopyButton value={person.link} />
                      </span>
                    </Table.Cell>
                    <Table.Cell>
                      <span className="flex flex-wrap gap-2">
                        <Button
                          size="sm"
                          variant="tertiary"
                          {...actions.props(`rotate:${person.login}`)}
                          onPress={() => {
                            if (
                              !confirm(
                                `Выдать «${person.display_name}» новую ссылку? Старая перестанет работать сразу.`,
                              )
                            )
                              return;
                            actions.run(`rotate:${person.login}`, async () => {
                              const fresh = await post<{ link: string }>(
                                `/api/admin/people/${person.login}/rotate`,
                              );
                              patch(person.login, { link: fresh.link });
                              toast.success("Новая ссылка готова — отправьте её человеку");
                            });
                          }}
                        >
                          Новая ссылка
                        </Button>
                        {/* Себя администратор отключить не может — сервер это запрещает,
                            поэтому и кнопку ему не показываем. */}
                        {person.role === "admin" ? null : (
                          <Button
                            size="sm"
                            variant={person.disabled ? "secondary" : "danger-soft"}
                            {...actions.props(`disable:${person.login}`)}
                            onPress={() => {
                              const disabled = !person.disabled;
                              if (
                                disabled &&
                                !confirm(`Отключить «${person.display_name}»? Ссылка перестанет пускать.`)
                              )
                                return;
                              actions.run(`disable:${person.login}`, async () => {
                                await post(`/api/admin/people/${person.login}/disable`, { disabled });
                                patch(person.login, { disabled });
                              });
                            }}
                          >
                            {person.disabled ? "Включить" : "Отключить"}
                          </Button>
                        )}
                      </span>
                    </Table.Cell>
                  </Table.Row>
                )}
              </Table.Body>
            </Table.Content>
          </Table.ScrollContainer>
        </Table>
      ) : (
        <p className="text-muted py-6 text-center text-sm">В базе пока никого нет.</p>
      )}

      <AddPerson
        onAdded={(person) =>
          state.setData((current) =>
            current ? { people: [...current.people, person] } : { people: [person] },
          )
        }
      />
    </div>
  );
}
