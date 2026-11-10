import { useState } from 'react';
import { api } from './api';
import { useCollection } from './collections';
import { Issues, Pager, date, shortId } from './components';
import type { RecordData } from './types';

export default function PlanHistory({
  kind,
  path,
  busy,
  onLoad,
}: {
  kind: string;
  path: string;
  busy: boolean;
  onLoad: (plan: RecordData) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const page = useCollection('candidates', { kind }, 5, expanded);
  const [loadingPlan, setLoadingPlan] = useState(false);
  const [error, setError] = useState('');
  const load = async (id: string) => {
    setLoadingPlan(true);
    setError('');
    try {
      onLoad(await api(`${path}/${id}`));
    } catch (failure) {
      setError((failure as Error).message);
    } finally {
      setLoadingPlan(false);
    }
  };
  return (
    <details className="detail-section" onToggle={(event) => setExpanded(event.currentTarget.open)}>
      <summary>历史计划</summary>
      <Issues issues={[page.error, error].filter(Boolean)} />
      {page.loading && <p role="status">正在读取历史计划…</p>}
      {!page.loading && !page.error && !page.items.length && (
        <p className="muted">暂无历史计划。</p>
      )}
      {page.items.map((item) => (
        <div className="segment-row" key={item.id}>
          <span>
            <strong>
              {item.state === 'applied' ? '已应用' : '待处理'} · {date(item.created_at)}
            </strong>
            <small>
              {[
                item.evidence?.book_title,
                item.evidence?.base_source_name,
                item.evidence?.source_name,
                item.evidence?.source_count > 1 ? `共 ${item.evidence.source_count} 份来源` : '',
              ]
                .filter(Boolean)
                .join(' · ') || `来源 ${shortId(item.source_id)}`}
            </small>
          </span>
          <button
            className="button small"
            disabled={busy || loadingPlan}
            onClick={() => void load(item.id)}
          >
            打开计划
          </button>
        </div>
      ))}
      <Pager page={page.page} total={page.total} size={page.size} onChange={page.setPage} />
    </details>
  );
}
