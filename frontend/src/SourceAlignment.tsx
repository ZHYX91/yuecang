import { useState } from 'react';
import { api, post } from './api';
import { Empty, Issues, Pager, shortId, usePagination } from './components';
import { SourceResource } from './SourcePanel';
import { useWorkbench } from './store';
import { sourceSpansToSelections, type Block, type RecordData, type SelectedSpan } from './types';

const kindLabels: Record<string, string> = {
  exact_duplicate: '相同文字',
  different_text: '文字不同',
  left_only: '仅左侧',
  right_only: '仅右侧',
  ambiguous: '对齐关系待确认',
};

type AlignedSide = { selections: SelectedSpan[]; resources: Map<string, RecordData[]> };
type AlignmentRow = {
  id: string;
  kind: string;
  reasons: string[];
  left: AlignedSide;
  right: AlignedSide;
};

function AlignmentSide({
  side,
  label,
  busy,
  add,
}: {
  side: AlignedSide;
  label: string;
  busy: boolean;
  add: (spans: SelectedSpan[]) => void;
}) {
  return (
    <>
      <small>
        {label}已归属范围 · {side.selections.length} 段
      </small>
      <div className="alignment-ranges">
        {side.selections.map((span) => (
          <div key={`${span.block_id}:${span.start}:${span.end}`}>
            <small>
              块 {shortId(span.block_id)} · 字符 {span.start}–{span.end}
            </small>
            <pre>{span.text || '（无文字）'}</pre>
            {side.resources.get(span.block_id)?.map((resource, index) => (
              <SourceResource
                key={`${resource.sha256}:${index}`}
                sourceId={span.source_id}
                resource={resource}
              />
            ))}
          </div>
        ))}
      </div>
      <button
        className="button small"
        disabled={busy || !side.selections.length}
        onClick={() => add(side.selections)}
      >
        采用{label}范围
      </button>
    </>
  );
}

export default function SourceAlignment({
  bookId,
  editionId,
  leftId,
  rightId,
  add,
}: {
  bookId: string;
  editionId: string;
  leftId: string;
  rightId: string;
  add: (spans: SelectedSpan[]) => void;
}) {
  const { run, busy } = useWorkbench();
  const [report, setReport] = useState<{ rows: AlignmentRow[]; incomplete: boolean } | null>(null);
  const pagination = usePagination(report?.rows || [], 10);
  const align = async () => {
    setReport(null);
    pagination.setPage(0);
    const result = await run('', async () => {
      const aligned = await post('/api/alignment', {
        book_id: bookId,
        edition_id: editionId,
        left_source_id: leftId,
        right_source_id: rightId,
      });
      if (!aligned.rows.length) return { rows: [], incomplete: aligned.incomplete };
      const [left, right] = await Promise.all([
        api(`/api/sources/${leftId}`),
        api(`/api/sources/${rightId}`),
      ]);
      const leftResources = resourcesByBlock(left.blocks);
      const rightResources = resourcesByBlock(right.blocks);
      return {
        incomplete: aligned.incomplete,
        rows: aligned.rows.map((row: RecordData) => ({
          id: row.id,
          kind: row.kind,
          reasons: row.reasons,
          left: { selections: sourceSpansToSelections(row.left, [left]), resources: leftResources },
          right: {
            selections: sourceSpansToSelections(row.right, [right]),
            resources: rightResources,
          },
        })),
      };
    });
    if (result) setReport(result);
  };
  return (
    <section className="card source-alignment">
      <div className="card-heading">
        <div>
          <h2>同一版本的内容对齐</h2>
          <p className="muted">
            只比较已经归属到目标图书和版本的范围。相同文字可择一，差异和不确定范围由你决定。
          </p>
        </div>
        <button
          className="button"
          disabled={busy > 0 || !bookId || !editionId || !leftId || !rightId || leftId === rightId}
          onClick={() => void align()}
        >
          对齐已归属内容
        </button>
      </div>
      {!editionId && (
        <p className="field-help">请先选择目标版本，并为两份来源保存明确的片段归属。</p>
      )}
      {report && (
        <>
          {report.incomplete && (
            <Issues issues={['本次对齐达到处理上限，未覆盖的范围须另行核对。']} />
          )}
          {pagination.visible.map((row) => (
            <article className="difference-card" key={row.id}>
              <div className="block-heading">
                <strong>{kindLabels[row.kind] || row.kind}</strong>
                <span>{(row.reasons || []).join('；')}</span>
              </div>
              <div className="diff-grid">
                <div className="diff-before">
                  <AlignmentSide side={row.left} label="左侧" busy={busy > 0} add={add} />
                </div>
                <div className="diff-after">
                  <AlignmentSide side={row.right} label="右侧" busy={busy > 0} add={add} />
                </div>
              </div>
            </article>
          ))}
          {!report.rows?.length && (
            <Empty title="暂无可对齐的已归属范围">
              <p>先将两份来源中的相关片段归属到相同图书和版本，再重试。</p>
            </Empty>
          )}
          <Pager
            page={pagination.page}
            total={report.rows?.length || 0}
            size={pagination.size}
            onChange={pagination.setPage}
          />
        </>
      )}
    </section>
  );
}

function resourcesByBlock(blocks: Block[]): Map<string, RecordData[]> {
  return new Map(
    blocks.flatMap((block) => {
      const resources = (block.resources || []).filter((resource: RecordData) => resource.sha256);
      return resources.length ? [[block.id, resources] as const] : [];
    }),
  );
}
