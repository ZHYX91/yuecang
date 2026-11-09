import { useEffect, useState } from 'react';
import { api } from './api';
import { useWorkbench } from './store';
import { normalizeRecord, type RecordData } from './types';

type CollectionPage = { items: RecordData[]; total: number; next_offset: number | null };
const emptyPage: CollectionPage = { items: [], total: 0, next_offset: null };

/** A server page is authoritative; never infer offsets from a merged cache. */
export function useCollection(
  collection: string,
  filters: Record<string, string | undefined> = {},
  size = 20,
  enabled = true,
) {
  const { state } = useWorkbench();
  const parameters = new URLSearchParams({ limit: String(size) });
  for (const [key, value] of Object.entries(filters)) {
    if (value) parameters.set(key, value);
  }
  const scope = `/api/${collection}?${parameters}`;
  const [position, setPosition] = useState({ scope, offset: 0 });
  const offset = position.scope === scope ? position.offset : 0;
  const url = `${scope}&offset=${offset}`;
  const eventId = state.event_id;
  const [result, setResult] = useState<{
    scope: string;
    url: string;
    eventId: number | undefined;
    data: CollectionPage;
    error: string;
  } | null>(null);
  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    void api<CollectionPage>(url, { signal: controller.signal })
      .then((value) => {
        if (controller.signal.aborted) return;
        if (offset > 0 && offset >= value.total) {
          setPosition({ scope, offset: Math.max(0, Math.ceil(value.total / size) - 1) * size });
          return;
        }
        setResult({
          scope,
          url,
          eventId,
          data: { ...value, items: value.items.map(normalizeRecord) },
          error: '',
        });
      })
      .catch((error) => {
        if (controller.signal.aborted) return;
        setResult({
          scope,
          url,
          eventId,
          data: emptyPage,
          error: error instanceof Error ? error.message : '无法读取列表',
        });
      });
    return () => controller.abort();
  }, [enabled, scope, url, eventId, offset, size]);
  const visible = enabled && result?.url === url ? result : null;
  const data = visible?.data ?? {
    ...emptyPage,
    total: enabled && result?.scope === scope ? result.data.total : 0,
  };
  return {
    ...data,
    page: Math.floor(offset / size),
    setPage: (page: number) => setPosition({ scope, offset: page * size }),
    size,
    loading: enabled && (!visible || visible.eventId !== eventId),
    error: visible?.eventId === eventId ? visible?.error || '' : '',
  };
}
