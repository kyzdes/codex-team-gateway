import type { RequestItem } from "../types";
import { Section } from "./Steps";

/**
 * Человеку показываем не диф, а пары «было → стало» по тексту: он оценивает
 * формулировки, а не код. Список файлов прячем под сворачивалку.
 */
export function RequestChanges({ request }: { request: RequestItem }) {
  if (!request.text_changes?.length) return null;
  return (
    <Section title="Что изменилось в тексте">
      <div className="flex flex-col gap-2">
        {request.text_changes.slice(0, 12).map((change, index) => (
          <div key={index} className="border-separator overflow-hidden rounded-lg border text-sm">
            <div className="bg-surface-secondary text-muted px-3 py-1.5 text-[11.5px]">
              {change.file}
            </div>
            {change.before ? (
              <div className="text-danger px-3 py-2 break-words whitespace-pre-wrap line-through">
                {change.before}
              </div>
            ) : null}
            {change.after ? (
              <div className="text-success border-separator border-t px-3 py-2 break-words whitespace-pre-wrap">
                {change.after}
              </div>
            ) : null}
          </div>
        ))}
      </div>
      {request.files?.length ? (
        <details className="text-muted text-sm">
          <summary className="cursor-pointer">Затронуто файлов: {request.files.length}</summary>
          <ul className="mt-2 list-inside list-disc font-mono text-xs">
            {request.files.map((file) => (
              <li key={file}>{file}</li>
            ))}
          </ul>
        </details>
      ) : null}
    </Section>
  );
}
