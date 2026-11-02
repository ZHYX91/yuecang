import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { api, ApiError, initSession } from './api';
import { emptyState, normalizeRecord, type RecordData, type WorkbenchState } from './types';

type Notice = { id: number; kind: 'success' | 'error' | 'info'; message: string };
interface Store {
  state: WorkbenchState;
  ready: boolean;
  connected: boolean;
  loading: boolean;
  busy: number;
  refresh: () => Promise<void>;
  run: <T>(message: string, action: () => Promise<T>) => Promise<T | undefined>;
  notify: (message: string, kind?: Notice['kind']) => void;
  hasUnsavedEdits: boolean;
  setHasUnsavedEdits: (value: boolean) => void;
  loadMore: (collection: string) => Promise<void>;
}
const Context = createContext<Store | null>(null);
export function WorkbenchProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<WorkbenchState>(emptyState);
  const [ready, setReady] = useState(false);
  const [connected, setConnected] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(0);
  const [hasUnsavedEdits, setHasUnsavedEdits] = useState(false);
  const [notices, setNotices] = useState<Notice[]>([]);
  const refreshing = useRef<Promise<void> | null>(null);
  const notifiedConnection = useRef(false);
  const notify = useCallback((message: string, kind: Notice['kind'] = 'info') => {
    const id = Date.now() + Math.random();
    setNotices((previous) => [...previous.slice(-1), { id, kind, message }]);
    if (kind !== 'error')
      window.setTimeout(
        () => setNotices((previous) => previous.filter((item) => item.id !== id)),
        4000,
      );
  }, []);
  const refresh = useCallback(async () => {
    if (refreshing.current) return refreshing.current;
    refreshing.current = (async () => {
      try {
        const next = await api<Partial<WorkbenchState>>('/api/state');
        setState((previous) => {
          const normalized = { ...emptyState, ...next } as WorkbenchState;
          for (const [key, value] of Object.entries(normalized)) {
            if (Array.isArray(value)) {
              const rows = value.map(normalizeRecord);
              if (next.totals?.[key] > rows.length && previous[key]?.length > rows.length) {
                const known = new Set(rows.map((item) => item.id));
                normalized[key] = [
                  ...rows,
                  ...previous[key].filter((item: RecordData) => !known.has(item.id)),
                ].slice(0, next.totals[key]);
              } else normalized[key] = rows;
            }
          }
          return normalized;
        });
        setConnected(true);
        setReady(true);
        notifiedConnection.current = false;
      } catch (error) {
        setConnected(false);
        if (!notifiedConnection.current) {
          notify(error instanceof Error ? error.message : '无法连接本地服务。', 'error');
          notifiedConnection.current = true;
        }
      } finally {
        setLoading(false);
        refreshing.current = null;
      }
    })();
    return refreshing.current;
  }, [notify]);
  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') void refresh();
    }, 6000);
    let debounce = 0;
    const update = () => {
      clearTimeout(debounce);
      debounce = window.setTimeout(() => void refresh(), 250);
    };
    let events: EventSource | undefined;
    let disposed = false;
    void initSession()
      .then(() => {
        if (disposed) return;
        events = new EventSource('/api/events');
        events.onmessage = update;
        events.addEventListener('state', update);
        events.addEventListener('update', update);
      })
      .catch(() => {
        /* The state refresh reports session failures; polling remains available. */
      });
    const visible = () => {
      if (document.visibilityState === 'visible') void refresh();
    };
    document.addEventListener('visibilitychange', visible);
    return () => {
      disposed = true;
      clearInterval(timer);
      clearTimeout(debounce);
      events?.close();
      document.removeEventListener('visibilitychange', visible);
    };
  }, [refresh]);
  const run = useCallback(
    async <T,>(message: string, action: () => Promise<T>): Promise<T | undefined> => {
      setBusy((value) => value + 1);
      try {
        const result = await action();
        await refresh();
        if (message) notify(message, 'success');
        return result;
      } catch (error) {
        const suffix =
          error instanceof ApiError && error.status === 409
            ? ' 当前输入已变化，你的编辑仍保留；请比较最新版本后再处理。'
            : '';
        notify((error instanceof Error ? error.message : '操作失败，请重试。') + suffix, 'error');
        return undefined;
      } finally {
        setBusy((value) => value - 1);
      }
    },
    [notify, refresh],
  );
  const loadMore = useCallback(
    async (collection: string) => {
      setBusy((value) => value + 1);
      try {
        const data = await api<{ items: RecordData[]; total: number }>(
          `/api/${collection}?offset=${state[collection]?.length || 0}&limit=500`,
        );
        setState((previous) => {
          const rows = previous[collection] || [];
          const known = new Set(rows.map((row: RecordData) => row.id));
          return {
            ...previous,
            [collection]: [
              ...rows,
              ...data.items.map(normalizeRecord).filter((item) => !known.has(item.id)),
            ],
            totals: { ...previous.totals, [collection]: data.total },
          };
        });
      } catch (error) {
        notify(error instanceof Error ? error.message : '无法载入下一批记录。', 'error');
      } finally {
        setBusy((value) => value - 1);
      }
    },
    [state, notify],
  );
  return (
    <Context.Provider
      value={{
        state,
        ready,
        connected,
        loading,
        busy,
        refresh,
        run,
        notify,
        hasUnsavedEdits,
        setHasUnsavedEdits,
        loadMore,
      }}
    >
      {children}
      <div className="notice-stack" aria-live="polite">
        {notices.map((notice) => (
          <div
            key={notice.id}
            className={`notice ${notice.kind}`}
            role={notice.kind === 'error' ? 'alert' : 'status'}
          >
            <span>{notice.message}</span>
            <button
              aria-label="关闭提示"
              onClick={() => setNotices((items) => items.filter((item) => item.id !== notice.id))}
            >
              ×
            </button>
          </div>
        ))}
      </div>
    </Context.Provider>
  );
}
export function useWorkbench() {
  const context = useContext(Context);
  if (!context) throw new Error('WorkbenchProvider required');
  return context;
}
