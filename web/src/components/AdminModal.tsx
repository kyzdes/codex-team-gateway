import { Modal, Tabs } from "@heroui/react";
import type { MetaPayload } from "../types";
import { useAdminData } from "./admin/shared";
import { JournalTab } from "./admin/JournalTab";
import { PeopleTab } from "./admin/PeopleTab";
import { ReadinessTab } from "./admin/ReadinessTab";
import { SettingsTab } from "./admin/SettingsTab";
import { UsageTab } from "./admin/UsageTab";

/**
 * Содержимое админки вынесено отдельно, потому что монтируется только на
 * открытой модалке: так каждая вкладка тянет состояние сервера в момент
 * открытия, а закрытая админка не держит ни одного запроса.
 */
function AdminTabs({ onPausedChange }: { onPausedChange: (paused: boolean) => void }) {
  const meta = useAdminData<MetaPayload>("/api/meta");

  return (
    <Tabs>
      <Tabs.ListContainer>
        <Tabs.List aria-label="Разделы админки">
          <Tabs.Tab id="readiness">
            Готовность
            <Tabs.Indicator />
          </Tabs.Tab>
          <Tabs.Tab id="usage">
            Расход
            <Tabs.Indicator />
          </Tabs.Tab>
          <Tabs.Tab id="people">
            Люди
            <Tabs.Indicator />
          </Tabs.Tab>
          <Tabs.Tab id="journal">
            Журнал
            <Tabs.Indicator />
          </Tabs.Tab>
          <Tabs.Tab id="settings">
            Настройки
            <Tabs.Indicator />
          </Tabs.Tab>
        </Tabs.List>
      </Tabs.ListContainer>

      <Tabs.Panel className="pt-4" id="readiness">
        <ReadinessTab />
      </Tabs.Panel>
      <Tabs.Panel className="pt-4" id="usage">
        <UsageTab />
      </Tabs.Panel>
      <Tabs.Panel className="pt-4" id="people">
        <PeopleTab />
      </Tabs.Panel>
      <Tabs.Panel className="pt-4" id="journal">
        <JournalTab statuses={meta.data?.statuses ?? {}} />
      </Tabs.Panel>
      <Tabs.Panel className="pt-4" id="settings">
        <SettingsTab
          error={meta.error}
          meta={meta.data}
          onPausedChange={(paused) => {
            meta.setData((current) => (current ? { ...current, paused } : current));
            // Баннер «приём приостановлен» живёт состоянием App: без этого
            // администратор, щёлкнувший переключатель, не видел собственную
            // паузу до переключения вкладки браузера.
            onPausedChange(paused);
          }}
          onRetry={meta.reload}
        />
      </Tabs.Panel>
    </Tabs>
  );
}

export function AdminModal({
  isOpen,
  onClose,
  onPausedChange,
}: {
  isOpen: boolean;
  onClose: () => void;
  onPausedChange: (paused: boolean) => void;
}) {
  return (
    <Modal
      isOpen={isOpen}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <Modal.Backdrop>
        <Modal.Container>
          <Modal.Dialog className="sm:max-w-[860px]">
            <Modal.CloseTrigger />
            <Modal.Header>
              <Modal.Heading>Админка</Modal.Heading>
            </Modal.Header>
            {/* Высоту держим руками, иначе модалка прыгает при переключении вкладок.
                На низком экране запас меньше: тело модалки и так прокручивается. */}
            <Modal.Body className="min-h-[240px] sm:min-h-[380px]">
              {isOpen ? <AdminTabs onPausedChange={onPausedChange} /> : null}
            </Modal.Body>
          </Modal.Dialog>
        </Modal.Container>
      </Modal.Backdrop>
    </Modal>
  );
}
