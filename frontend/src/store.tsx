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
  remember: (collection: string, items: RecordData[]) => void;
}
const Context = createContext<Store | null>(null);
function withRemembered(rows: RecordData[], remembered: RecordData[]) {
  const ids = new Set(rows.map((item) => item.id));
  return [...rows, ...remembered.filter((item) => !ids.has(item.id))];
}

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
  const snapshot = useRef<WorkbenchState>(emptyState);
  const retained = useRef<Record<string, RecordData[]>>({});
  const remember = useCallback((collection: string, items: RecordData[]) => {
    if (!items.length || !['books', 'sources', 'editions', 'revisions'].includes(collection))
      return;
    const recent: RecordData[] = snapshot.current[collection] || [];
    const current = new Map(recent.map((item) => [item.id, item]));
    const values = items.map((item) => current.get(item.id) ?? normalizeRecord(item));
    const remembered = withRemembered(values, retained.current[collection] || []).slice(0, 500);
    retained.current[collection] = remembered;
    const rows = withRemembered(recent, remembered);
    setState((previous) => ({ ...previous, [collection]: rows }));
  }, []);
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
        const normalized = { ...emptyState, ...next } as WorkbenchState;
        for (const [key, value] of Object.entries(normalized)) {
          if (Array.isArray(value)) normalized[key] = value.map(normalizeRecord);
        }
        snapshot.current = normalized;
        const visible = { ...normalized };
        for (const [collection, remembered] of Object.entries(retained.current)) {
          const rows: RecordData[] = normalized[collection];
          const recent = new Map(rows.map((item) => [item.id, item]));
          retained.current[collection] = remembered.map((item) => recent.get(item.id) ?? item);
          visible[collection] = withRemembered(rows, remembered);
        }
        setState(visible);
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
        if (refreshing.current) await refreshing.current;
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
        remember,
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
