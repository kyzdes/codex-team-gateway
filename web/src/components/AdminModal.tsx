import { useEffect, useState } from "react";
import { Alert, Button, Modal, Separator, Spinner } from "@heroui/react";
import { api } from "../api";
import type { AdminOverview } from "../types";

const DEPLOY_MODE: Record<string, string> = {
  dokploy: "через Dokploy API",
  healthcheck: "по health-адресу сайта",
  none: "не настроено",
};

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="grid grid-cols-[150px_1fr] gap-x-3 gap-y-1 text-sm">
      <dt className="text-muted">{label}</dt>
      <dd className="break-words">{value}</dd>
    </div>
  );
}

export function AdminModal({ isOpen, onClose }: { isOpen: boolean; onClose: () => void }) {
  const [data, setData] = useState<AdminOverview | null>(null);
  const [copied, setCopied] = useState("");

  // Состояние тянем при каждом открытии: оно меняется (клон, токен, деплой).
  useEffect(() => {
    if (!isOpen) return;
    setData(null);
    api<AdminOverview>("/api/admin/overview")
      .then(setData)
      .catch((error: Error) => setData({ error: error.message }));
  }, [isOpen]);

  return (
    <Modal
      isOpen={isOpen}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <Modal.Backdrop>
        <Modal.Container>
          <Modal.Dialog className="sm:max-w-[640px]">
            <Modal.CloseTrigger />
            <Modal.Header>
              <Modal.Heading>Админка</Modal.Heading>
            </Modal.Header>
            <Modal.Body className="flex flex-col gap-4">
              {!data ? (
                <div className="text-muted flex items-center gap-2 text-sm">
                  <Spinner className="size-4" /> Загружаю состояние…
                </div>
              ) : null}

              {data?.error ? (
                <Alert status="danger">
                  <Alert.Indicator />
                  <Alert.Content>
                    <Alert.Title>Не удалось получить состояние</Alert.Title>
                    <Alert.Description>{data.error}</Alert.Description>
                  </Alert.Content>
                </Alert>
              ) : null}

              {data?.config_problems?.length ? (
                <Alert status="danger">
                  <Alert.Indicator />
                  <Alert.Content>
                    <Alert.Title>Не хватает настроек</Alert.Title>
                    <Alert.Description>
                      <ul className="list-inside list-disc">
                        {data.config_problems.map((problem) => (
                          <li key={problem}>{problem}</li>
                        ))}
                      </ul>
                    </Alert.Description>
                  </Alert.Content>
                </Alert>
              ) : null}

              {data && !data.error ? (
                <dl className="flex flex-col gap-1.5">
                  <Row
                    label="Репозиторий"
                    value={
                      data.github?.ok
                        ? `${data.github.repo} (${data.github.can_push ? "есть право записи" : "НЕТ права записи"})`
                        : (data.github?.error ?? "—")
                    }
                  />
                  <Row
                    label="Основная ветка"
                    value={
                      data.repo?.ok
                        ? `${data.repo.base} · ${data.repo.head} · ${data.repo.last_commit}`
                        : (data.repo?.error ?? "—")
                    }
                  />
                  <Row
                    label="Песочница агента"
                    value={
                      data.sandbox
                        ? `${data.sandbox.mode}, сеть ${data.sandbox.network ? "включена" : "выключена"}, модель ${data.sandbox.model}`
                        : "—"
                    }
                  />
                  <Row
                    label="Слежение за выкаткой"
                    value={DEPLOY_MODE[data.deploy_mode ?? "none"] ?? "—"}
                  />
                  <Row
                    label="Параллельных заявок"
                    value={String(data.runtime?.max_concurrent ?? "—")}
                  />
                  <Row
                    label="Локальная копия"
                    value={
                      data.runtime?.repo_ready ? "готова" : data.runtime?.repo_error || "готовится"
                    }
                  />
                </dl>
              ) : null}

              {data?.access_links?.length ? (
                <>
                  <Separator />
                  <div className="flex flex-col gap-2">
                    <h3 className="text-muted text-xs font-semibold tracking-wider uppercase">
                      Персональные ссылки
                    </h3>
                    {data.access_links.map((person) => (
                      <div key={person.login} className="flex items-center gap-2 text-sm">
                        <span className="w-32 flex-none truncate">
                          {person.display_name}
                          {person.role === "admin" ? " (админ)" : ""}
                        </span>
                        <code className="text-muted flex-1 truncate text-xs">{person.link}</code>
                        <Button
                          size="sm"
                          variant="ghost"
                          onPress={() => {
                            void navigator.clipboard.writeText(person.link);
                            setCopied(person.login);
                          }}
                        >
                          {copied === person.login ? "Скопировано" : "Копировать"}
                        </Button>
                      </div>
                    ))}
                  </div>
                </>
              ) : null}
            </Modal.Body>
          </Modal.Dialog>
        </Modal.Container>
      </Modal.Backdrop>
    </Modal>
  );
}
