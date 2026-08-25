import { Button, Card, Link } from "@heroui/react";
import type { Me } from "../types";

export function Gate({ message }: { message?: string }) {
  return (
    <div className="grid min-h-full place-items-center p-6">
      <Card className="max-w-[420px] text-center">
        <Card.Header className="items-center">
          <span className="bg-accent-soft text-accent mx-auto grid size-11 place-items-center rounded-xl text-lg">
            ✳
          </span>
          <Card.Title>Нужна персональная ссылка</Card.Title>
          <Card.Description>
            Этот раздел открывается по личной ссылке, которую выдаёт администратор. Откройте её один
            раз — дальше браузер запомнит доступ.
          </Card.Description>
        </Card.Header>
        {message ? (
          <Card.Content>
            <p className="text-danger text-sm">{message}</p>
          </Card.Content>
        ) : null}
      </Card>
    </div>
  );
}

export function TopBar({ me, onOpenAdmin }: { me: Me; onOpenAdmin: () => void }) {
  return (
    <header className="border-separator bg-background/85 sticky top-0 z-20 flex items-center justify-between gap-4 border-b px-4 py-3 backdrop-blur-md">
      <div className="flex min-w-0 items-center gap-3">
        <span className="bg-accent-soft text-accent grid size-8 flex-none place-items-center rounded-lg">
          ✳
        </span>
        <span className="flex min-w-0 flex-col leading-tight">
          <strong className="truncate text-[15px] font-semibold">{me.brand.name}</strong>
          <small className="text-muted hidden truncate text-xs sm:block">{me.brand.subtitle}</small>
        </span>
      </div>
      <div className="flex flex-none items-center gap-3">
        {me.project.site ? (
          <Link
            className="text-sm"
            href={me.project.site}
            rel="noopener noreferrer"
            target="_blank"
          >
            Открыть сайт
          </Link>
        ) : null}
        {me.role === "admin" ? (
          <Button size="sm" variant="ghost" onPress={onOpenAdmin}>
            Админка
          </Button>
        ) : null}
        <span className="text-muted max-w-[110px] truncate text-sm">{me.display_name}</span>
      </div>
    </header>
  );
}
