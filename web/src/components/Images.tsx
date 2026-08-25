import { useCallback, useEffect, useRef, useState } from "react";
import { Button, Spinner, toast } from "@heroui/react";
import { fetchImage, uploadImage } from "../api";

export const MAX_IMAGES = 4;

interface Attachment {
  key: string;
  previewUrl: string;
  id?: string;
  failed?: boolean;
}

/**
 * Черновик картинок к сообщению: превью показываем сразу из локального файла,
 * а загрузка идёт фоном — человек не должен ждать сеть, чтобы дописать текст.
 */
export function useAttachments() {
  const [items, setItems] = useState<Attachment[]>([]);
  const counter = useRef(0);

  const add = useCallback((files: File[]) => {
    const pictures = files.filter((file) => file.type.startsWith("image/"));
    if (!pictures.length) return;

    setItems((current) => {
      const room = MAX_IMAGES - current.length;
      if (room <= 0) {
        toast.warning(`К одному сообщению можно приложить не больше ${MAX_IMAGES} картинок`);
        return current;
      }
      const accepted = pictures.slice(0, room);
      if (accepted.length < pictures.length) {
        toast.warning(`Взял только ${accepted.length}: больше ${MAX_IMAGES} картинок нельзя`);
      }
      const fresh = accepted.map((file) => {
        const key = `img-${(counter.current += 1)}`;
        void uploadImage(file)
          .then((id) => {
            setItems((now) => now.map((item) => (item.key === key ? { ...item, id } : item)));
          })
          .catch((error: Error) => {
            toast.danger(error.message);
            setItems((now) => now.map((item) => (item.key === key ? { ...item, failed: true } : item)));
          });
        return { key, previewUrl: URL.createObjectURL(file) };
      });
      return [...current, ...fresh];
    });
  }, []);

  const remove = useCallback((key: string) => {
    setItems((current) => {
      const gone = current.find((item) => item.key === key);
      if (gone) URL.revokeObjectURL(gone.previewUrl);
      return current.filter((item) => item.key !== key);
    });
  }, []);

  const reset = useCallback(() => {
    setItems((current) => {
      current.forEach((item) => URL.revokeObjectURL(item.previewUrl));
      return [];
    });
  }, []);

  return {
    items,
    add,
    remove,
    reset,
    ids: items.filter((item) => item.id).map((item) => item.id as string),
    uploading: items.some((item) => !item.id && !item.failed),
  };
}

/** Кнопка выбора файла + подсказка про вставку из буфера. */
export function AttachButton({ onPick, hint }: { onPick: (files: File[]) => void; hint?: boolean }) {
  const input = useRef<HTMLInputElement>(null);
  return (
    <span className="flex items-center gap-2">
      <input
        ref={input}
        accept="image/png,image/jpeg,image/gif,image/webp"
        className="hidden"
        multiple
        type="file"
        onChange={(event) => {
          onPick(Array.from(event.target.files ?? []));
          event.target.value = "";
        }}
      />
      <Button size="sm" variant="tertiary" onPress={() => input.current?.click()}>
        Прикрепить картинку
      </Button>
      {hint ? <span className="text-muted hidden text-xs sm:inline">или вставьте скриншот в поле</span> : null}
    </span>
  );
}

export function AttachmentStrip({
  items,
  onRemove,
}: {
  items: Attachment[];
  onRemove: (key: string) => void;
}) {
  if (!items.length) return null;
  return (
    <div className="flex flex-wrap gap-2">
      {items.map((item) => (
        <div
          key={item.key}
          className="border-separator bg-surface-secondary relative size-20 overflow-hidden rounded-lg border"
        >
          <img alt="Вложение" className="size-full object-cover" src={item.previewUrl} />
          {!item.id && !item.failed ? (
            <span className="bg-backdrop/60 absolute inset-0 grid place-items-center">
              <Spinner className="size-4" />
            </span>
          ) : null}
          {item.failed ? (
            <span className="bg-danger-soft text-danger absolute inset-0 grid place-items-center text-[11px]">
              не загрузилась
            </span>
          ) : null}
          <button
            aria-label="Убрать картинку"
            className="bg-backdrop/70 absolute top-1 right-1 grid size-5 place-items-center rounded-full text-xs text-white"
            type="button"
            onClick={() => onRemove(item.key)}
          >
            ×
          </button>
        </div>
      ))}
    </div>
  );
}

/** Картинка заявки: доступ закрыт токеном, поэтому грузим её запросом. */
export function AuthedImage({ path, alt }: { path: string; alt: string }) {
  const [url, setUrl] = useState("");

  useEffect(() => {
    let objectUrl = "";
    let alive = true;
    fetchImage(path)
      .then((created) => {
        objectUrl = created;
        if (alive) setUrl(created);
        else URL.revokeObjectURL(created);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [path]);

  if (!url) {
    return <span className="bg-surface-secondary block size-24 animate-pulse rounded-lg" />;
  }
  return (
    <a href={url} rel="noopener noreferrer" target="_blank" title="Открыть в полном размере">
      <img
        alt={alt}
        className="border-separator size-24 rounded-lg border object-cover transition-opacity hover:opacity-80"
        src={url}
      />
    </a>
  );
}

/** Обработчик вставки скриншота из буфера — главный способ приложить картинку. */
export function pasteHandler(add: (files: File[]) => void) {
  return (event: React.ClipboardEvent) => {
    const files = Array.from(event.clipboardData.files ?? []);
    const pictures = files.filter((file) => file.type.startsWith("image/"));
    if (!pictures.length) return;
    event.preventDefault();
    add(pictures);
  };
}

export function dropHandlers(add: (files: File[]) => void) {
  return {
    onDragOver: (event: React.DragEvent) => {
      if (event.dataTransfer.types.includes("Files")) event.preventDefault();
    },
    onDrop: (event: React.DragEvent) => {
      const files = Array.from(event.dataTransfer.files ?? []);
      if (!files.some((file) => file.type.startsWith("image/"))) return;
      event.preventDefault();
      add(files);
    },
  };
}
