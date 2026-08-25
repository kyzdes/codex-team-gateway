import { useEffect, useRef } from "react";
import { Separator } from "@heroui/react";
import type { RequestItem } from "../types";
import { statusOf, steps } from "../status";

/** Шкала «где сейчас заявка»: единственная навигация по длинному процессу. */
export function Steps({ request }: { request: RequestItem }) {
  const meta = statusOf(request);
  const broken = meta.step === -1;
  const current = useRef<HTMLDivElement>(null);
  const labels = steps();

  // На узком экране шкала не помещается — подкручиваем к текущему шагу.
  useEffect(() => {
    current.current?.scrollIntoView({ block: "nearest", inline: "center" });
  }, [meta.step]);

  return (
    <div className="flex gap-1.5 overflow-x-auto px-5 py-4">
      {labels.map((label, index) => {
        const done = !broken && index < meta.step;
        const isCurrent = !broken && index === meta.step;
        return (
          <div
            key={label}
            ref={isCurrent ? current : undefined}
            className="min-w-[94px] flex-1 pr-2.5 text-[11.5px] whitespace-nowrap"
          >
            <div
              className={`mb-1.5 h-1 rounded-full ${
                broken && index === 0
                  ? "bg-danger"
                  : isCurrent
                    ? "bg-accent"
                    : done
                      ? "bg-accent/45"
                      : "bg-surface-secondary"
              }`}
            />
            <span className={isCurrent ? "text-foreground font-medium" : "text-muted"}>{label}</span>
          </div>
        );
      })}
    </div>
  );
}

/**
 * Полоса карточки заявки. Лежит рядом со шкалой, а не в каркасе RequestDetail,
 * чтобы части карточки не импортировали свой же каркас по кругу.
 */
export function Section({ title, children }: { title?: string; children: React.ReactNode }) {
  return (
    <>
      <Separator />
      <div className="flex flex-col gap-3 px-5 py-4">
        {title ? (
          <h3 className="text-muted text-xs font-semibold tracking-wider uppercase">{title}</h3>
        ) : null}
        {children}
      </div>
    </>
  );
}
