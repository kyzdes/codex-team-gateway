import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Toast, toast } from "@heroui/react";
import { Unauthorized, api, initToken, listen } from "./api";
import { ATTENTION, isPaused, loadMeta, statusOf, titleOf } from "./status";
import type { EventItem, Me, RequestItem, StreamPayload } from "./types";
import { Gate, TopBar } from "./components/Shell";
import { Composer } from "./components/Composer";
import { RequestList } from "./components/RequestList";
import { RequestDetail } from "./components/RequestDetail";
import { AdminModal } from "./components/AdminModal";

function applyTheme(accent: string) {
  const root = document.documentElement;
  if (accent) root.style.setProperty("--accent", accent);
  const dark = window.matchMedia("(prefers-color-scheme: dark)");
  const sync = () => {
    root.classList.toggle("dark", dark.matches);
    root.dataset.theme = dark.matches ? "dark" : "light";
  };
  sync();
  dark.addEventListener("change", sync);
}

export default function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [gate, setGate] = useState<string | null>(null);
  const [requests, setRequests] = useState<RequestItem[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [events, setEvents] = useState<EventItem[]>([]);
  const [adminOpen, setAdminOpen] = useState(false);
  const [paused, setPaused] = useState(false);
  const selectedRef = useRef<number | null>(null);
  selectedRef.current = selectedId;
  // Зеркало списка для потока событий: колбэк живёт дольше одного рендера.
  const requestsRef = useRef<RequestItem[]>([]);
  requestsRef.current = requests;
  // Было ли уже соединение с лентой: второй и последующие «hello» означают,
  // что связь рвалась.
  const connected = useRef(false);

  const fail = (error: unknown) => {
    if (error instanceof Unauthorized) {
      setGate("Ссылка больше не действует. Попросите новую у администратора.");
      setMe(null);
      return;
    }
    toast.danger((error as Error).message ?? "Что-то пошло не так");
  };

  const upsert = useCallback((request: RequestItem) => {
    setRequests((current) => {
      const index = current.findIndex((item) => item.id === request.id);
      if (index === -1) return [request, ...current];
      const next = [...current];
      next[index] = { ...next[index], ...request };
      return next;
    });
  }, []);

  const refresh = useCallback(async () => {
    const data = await api<{ requests: RequestItem[] }>("/api/requests");
    setRequests(data.requests);
  }, []);

  const select = useCallback(async (id: number) => {
    setSelectedId(id);
    try {
      const data = await api<{ request: RequestItem; events: EventItem[] }>(`/api/requests/${id}`);
      setEvents(data.events);
      upsert(data.request);
    } catch (error) {
      fail(error);
    }
  }, [upsert]);

  // Первичная загрузка. Токена в браузере может и не быть: вход мог случиться
  // по ссылке ?k=..., которую сервер уже обменял на cookie-сессию.
  useEffect(() => {
    const token = initToken();
    (async () => {
      try {
        const profile = await api<Me>("/api/me");
        applyTheme(profile.brand.accent);
        document.title = profile.brand.name;
        await loadMeta();
        setPaused(isPaused());
        setMe(profile);
        setGate(null);
        await refresh();
      } catch (error) {
        if (error instanceof Unauthorized) setGate(token ? "Ссылка больше не действует." : "");
        else setGate((error as Error).message);
      }
    })();
  }, [refresh]);

  // Живые обновления
  useEffect(() => {
    if (!me) return undefined;
    return listen((payload: StreamPayload) => {
      if (payload.type === "hello") {
        // Этим кадром начинается каждое соединение. Первый — обычный старт,
        // любой следующий значит, что лента прерывалась: пока её не было,
        // статусы уехали, а догнать их событиями уже нельзя.
        if (!connected.current) {
          connected.current = true;
          return;
        }
        void refresh().catch(fail);
        const opened = selectedRef.current;
        if (opened !== null) void select(opened);
      } else if (payload.type === "request") {
        // Уведомление считаем до setState: апдейтер обязан быть чистым, иначе
        // в StrictMode он выполняется дважды и уведомление дублируется.
        const previous = requestsRef.current.find((item) => item.id === payload.request.id);
        if (!previous || previous.status !== payload.request.status) notify(payload.request, me);
        upsert(payload.request);
      } else if (payload.type === "event" && payload.request_id === selectedRef.current) {
        setEvents((current) => [...current, payload]);
      }
    });
  }, [me, refresh, select, upsert]);

  useEffect(() => {
    if (!me) return undefined;
    const onVisible = () => {
      if (document.visibilityState !== "visible") return;
      document.title = me.brand.name;
      // Пока вкладка была в фоне, администратор мог поставить приём на паузу.
      void loadMeta().then(() => setPaused(isPaused()));
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [me]);

  if (!me) return <Gate message={gate ?? undefined} />;

  const selected = requests.find((item) => item.id === selectedId) ?? null;

  // Показать ошибку мало: вызывающий по молчанию считает отправку удачной и
  // чистит поле, а человек остаётся без своего текста. Поэтому пробрасываем.
  const act = async (path: string, body?: unknown) => {
    try {
      await api(path, { method: "POST", body: body ? JSON.stringify(body) : undefined });
    } catch (error) {
      fail(error);
      throw error;
    }
  };

  return (
    <div className="flex min-h-full flex-col">
      <Toast.Provider placement="bottom end" />
      <TopBar me={me} onOpenAdmin={() => setAdminOpen(true)} />
      <AdminModal
        isOpen={adminOpen}
        onClose={() => setAdminOpen(false)}
        onPausedChange={setPaused}
      />

      <main className="mx-auto grid w-full max-w-[1180px] flex-1 grid-cols-1 items-start gap-5 px-4 pt-5 pb-10 lg:grid-cols-[minmax(320px,400px)_1fr]">
        <section className={`flex flex-col gap-3 ${selected ? "hidden lg:flex" : "flex"}`}>
          {paused ? (
            <Alert className="bg-warning-soft" status="warning">
              <Alert.Indicator />
              <Alert.Content>
                <Alert.Title>Приём заявок приостановлен</Alert.Title>
                <Alert.Description>
                  Заявку примем и сохраним, но за работу возьмёмся, когда администратор снимет
                  паузу.
                </Alert.Description>
              </Alert.Content>
            </Alert>
          ) : null}
          <Composer
            onSubmit={async (text, images) => {
              try {
                const created = await api<RequestItem>("/api/requests", {
                  method: "POST",
                  body: JSON.stringify({ body: text, images }),
                });
                upsert(created);
                void select(created.id);
                if ("Notification" in window && Notification.permission === "default") {
                  void Notification.requestPermission();
                }
              } catch (error) {
                fail(error);
                throw error;
              }
            }}
          />
          <div className="mt-2 flex items-baseline justify-between px-1">
            <h2 className="text-muted text-xs font-semibold tracking-wider uppercase">
              {me.role === "admin" ? "Все заявки" : "Мои заявки"}
            </h2>
            <span className="text-muted text-xs">{requests.length || ""}</span>
          </div>
          <RequestList
            requests={requests}
            selectedId={selectedId}
            showAuthor={me.role === "admin"}
            onSelect={(id) => void select(id)}
          />
        </section>

        <section className={selected ? "block" : "hidden lg:block"}>
          {selected ? (
            <RequestDetail
              events={events}
              me={me}
              request={selected}
              onAnswer={async (text, images) => {
                await act(`/api/requests/${selected.id}/answer`, { text, images });
              }}
              onApprove={async () => {
                await act(`/api/requests/${selected.id}/approve`);
              }}
              onBack={() => setSelectedId(null)}
              onCancel={async () => {
                if (!confirm("Отменить заявку? Правка не попадёт на сайт.")) return;
                await act(`/api/requests/${selected.id}/cancel`);
                await refresh();
              }}
              onRetry={async () => {
                try {
                  const created = await api<RequestItem>(`/api/requests/${selected.id}/retry`, {
                    method: "POST",
                  });
                  await refresh();
                  void select(created.id);
                } catch (error) {
                  fail(error);
                }
              }}
            />
          ) : (
            <div className="border-separator text-muted grid min-h-[420px] place-content-center gap-2 rounded-xl border border-dashed text-center">
              <div className="text-2xl opacity-40">✳</div>
              <p>Выберите заявку слева, чтобы посмотреть, что происходит.</p>
            </div>
          )}
        </section>
      </main>
    </div>
  );
}

function notify(request: RequestItem, me: Me) {
  if (!ATTENTION.has(request.status)) return;
  document.title = `● ${me.brand.name}`;
  if (!("Notification" in window) || Notification.permission !== "granted") return;
  if (document.visibilityState === "visible") return;
  new Notification(me.brand.name, {
    body: `${statusOf(request).label}: ${titleOf(request)}`,
  });
}
