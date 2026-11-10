import { useEffect, useState } from 'react';
import { api } from './api';
import { AssignmentTargetFields, type AssignmentTarget } from './AssignmentTarget';
import { Issues, Pager, shortId } from './components';
import { useWorkbench } from './store';
import type { RecordData } from './types';

const pageSize = 10;
type AssignmentPage = { items: RecordData[]; total: number; blocks_sha: string };

export default function AssignmentEditor({ sourceId }: { sourceId: string }) {
  const { state, run, busy } = useWorkbench();
  const [page, setPage] = useState(0);
  const [expanded, setExpanded] = useState(false);
  const [result, setResult] = useState<{ key: string; data?: AssignmentPage; error?: string }>();
  const [editing, setEditing] = useState<{
    key: string;
    id: string;
    action: 'rebind' | 'remove';
  }>();
  const [target, setTarget] = useState<AssignmentTarget>({ book_id: '', edition_id: '' });
  const [reload, setReload] = useState(0);
  const key = JSON.stringify([sourceId, page, state.assignment_generation, reload]);
  useEffect(() => {
    if (!expanded || !sourceId) return;
    const controller = new AbortController();
    void api<AssignmentPage>(
      `/api/sources/${sourceId}/assignments?offset=${page * pageSize}&limit=${pageSize}`,
      { signal: controller.signal },
    )
      .then((data) => {
        if (controller.signal.aborted) return;
        if (page > 0 && page * pageSize >= data.total) {
          setPage(Math.max(0, Math.ceil(data.total / pageSize) - 1));
          return;
        }
        setResult({ key, data });
      })
      .catch((error) => {
        if (!controller.signal.aborted) setResult({ key, error: error.message });
      });
    return () => controller.abort();
  }, [expanded, key, page, sourceId]);

  const data = result?.key === key ? result.data : undefined;
  const error = result?.key === key ? result.error : undefined;
  const save = async (row: RecordData, action: 'rebind' | 'remove') => {
    if (!data) return;
    const expected = {
      source_id: row.source_id,
      block_id: row.block_id,
      start: row.start,
      end: row.end,
      book_id: row.book_id,
      edition_id: row.edition_id,
      blocks_sha: data.blocks_sha,
    };
    const response = await run(
      action === 'remove' ? '已解除所选区间归属。' : '已更新所选区间归属。',
      () =>
        api(`/api/assignments/${row.id}`, {
          method: action === 'remove' ? 'DELETE' : 'PATCH',
          body: JSON.stringify({ expected, ...(action === 'rebind' ? { target } : {}) }),
        }),
    );
    if (response) {
      setEditing(undefined);
      setReload((value) => value + 1);
    }
  };
  return (
    <details className="detail-section" onToggle={(event) => setExpanded(event.currentTarget.open)}>
      <summary>当前归属{data ? ` · ${data.total} 段` : ''}</summary>
      {expanded && !data && !error && <p role="status">正在读取归属…</p>}
      {error && <Issues issues={[error]} />}
      {data?.total === 0 && <p className="muted">这份来源暂无已确认的归属区间。</p>}
      {data?.items.map((row) => (
        <article className="difference-card" key={row.id}>
          <div className="block-heading">
            <strong>
              {row.book_title} · {row.edition_label}
            </strong>
            <span>
              块 {shortId(row.block_id)} · 字符 {row.start}–{row.end}
            </span>
          </div>
          <div className="alignment-ranges">
            <pre>{row.preview || '（无文字）'}</pre>
          </div>
          <div className="guided-actions">
            <button
              className="button small"
              disabled={busy > 0}
              onClick={() => {
                setTarget({ book_id: '', edition_id: '' });
                setEditing({ key, id: row.id, action: 'rebind' });
              }}
            >
              更正归属
            </button>
            <button
              className="button small"
              disabled={busy > 0}
              onClick={() => setEditing({ key, id: row.id, action: 'remove' })}
            >
              解除归属
            </button>
          </div>
          {editing && editing.id === row.id && editing.key === key && (
            <>
              {editing.action === 'rebind' ? (
                <AssignmentTargetFields
                  value={target}
                  onChange={setTarget}
                  version={state.event_id}
                />
              ) : (
                <p>解除上述字符区间与该图书版本的关联。已生成的整理稿仍可对照原文。</p>
              )}
              <div className="guided-actions">
                <button
                  className="button primary small"
                  disabled={
                    busy > 0 ||
                    (editing.action === 'rebind' && (!target.book_id || !target.edition_id))
                  }
                  onClick={() => void save(row, editing.action)}
                >
                  {editing.action === 'rebind' ? '保存更正' : '确认解除'}
                </button>
                <button className="button small" onClick={() => setEditing(undefined)}>
                  取消
                </button>
              </div>
            </>
          )}
        </article>
      ))}
      {data && <Pager page={page} total={data.total} size={pageSize} onChange={setPage} />}
    </details>
  );
}
