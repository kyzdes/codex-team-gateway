import { useEffect, useRef } from "react";
import type { EventItem } from "../types";
import { clockTime } from "../status";
import { Section } from "./Steps";

const BUBBLE: Record<string, string> = {
  agent: "bg-surface-secondary rounded-lg px-3 py-2 whitespace-pre-wrap",
  user: "bg-accent-soft rounded-lg px-3 py-2 whitespace-pre-wrap",
  error: "text-danger whitespace-pre-wrap",
};

/** Лента хода работы: пока заявка живая, это единственный признак движения. */
export function RequestFeed({ events }: { events: EventItem[] }) {
  const box = useRef<HTMLDivElement>(null);

  // Новые строки приходят снизу — держим прокрутку в конце ленты.
  useEffect(() => {
    if (box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [events.length]);

  if (!events.length) return null;
  return (
    <Section title="Ход работы">
      <div ref={box} className="flex max-h-80 flex-col gap-2 overflow-y-auto">
        {events.slice(-80).map((event, index) => (
          <div key={index} className="flex gap-2.5 text-sm leading-snug">
            <span className="text-muted flex-none pt-0.5 text-xs tabular-nums">
              {clockTime(event.ts)}
            </span>
            <span className={BUBBLE[event.kind] ?? "text-muted whitespace-pre-wrap"}>
              {event.text}
            </span>
          </div>
        ))}
      </div>
    </Section>
  );
}
