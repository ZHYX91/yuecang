import { useEffect, useState } from 'react';
import { api, post } from './api';
import { Badge, Empty, Issues, Pager, usePagination } from './components';
import { useWorkbench } from './store';
import { textOf, type RecordData } from './types';

function sliceText(text: string, start: number, end: number, limit?: number) {
  let index = 0;
  let result = '';
  for (const character of text) {
    if (index >= end || (limit !== undefined && index >= start + limit)) break;
    if (index >= start) result += character;
    index += 1;
  }
  return result;
}
export default function Comparison({ leftId, rightId }: { leftId: string; rightId: string }) {
  const { run, busy } = useWorkbench();
  const [result, setResult] = useState<RecordData | null>(null);
  const [texts, setTexts] = useState(['', '']);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  useEffect(() => {
    setResult(null);
    setTexts(['', '']);
    setExpanded(new Set());
  }, [leftId, rightId]);
  const pagination = usePagination<RecordData>(result?.opcodes || [], 10);
  const compare = async () => {
    const data = await run('来源差异分析已完成。', () =>
      Promise.all([
        post('/api/compare', { left_source_id: leftId, right_source_id: rightId }),
        api(`/api/sources/${leftId}`),
        api(`/api/sources/${rightId}`),
      ]),
    );
    if (data) {
      setResult(data[0]);
      setTexts(
        data
          .slice(1)
          .map((source) => (source.blocks || source.source?.blocks || []).map(textOf).join('\n')),
      );
      pagination.setPage(0);
    }
  };
  return (
    <section className="card comparison-results">
      <div className="card-heading">
        <div>
          <h2>来源差异</h2>
          <p className="muted">逐字差异辅助判断，内容相似不代表同一版本。</p>
        </div>
        <button
          className="button"
          disabled={!leftId || !rightId || leftId === rightId || busy > 0}
          onClick={() => void compare()}
        >
          比较左右来源
        </button>
      </div>
      {result && (
        <>
          <div className="compare-summary">
            <Badge status={result.automatic_equivalence ? 'ready' : 'waiting_review'}>
              {result.automatic_equivalence ? '原文逐字一致' : '存在差异，需确认版本关系'}
            </Badge>
            <span>
              左 {result.left_chars?.toLocaleString()} 字 · 右{' '}
              {result.right_chars?.toLocaleString()} 字 · {result.opcodes?.length || 0} 处差异
            </span>
          </div>
          {result.large_unmatched_regions > 0 && (
            <Issues
              issues={[
                `有 ${result.large_unmatched_regions} 个较大范围尚未可靠对齐，保留为待处理区间。`,
              ]}
            />
          )}
          {pagination.visible.map((difference, offset) => {
            const index = pagination.page * pagination.size + offset;
            const full = expanded.has(index);
            const left = difference.left_span || [0, 0];
            const right = difference.right_span || [0, 0];
            return (
              <article className="difference-card" key={index}>
                <div className="block-heading">
                  <span>
                    差异 {index + 1} ·{' '}
                    {(
                      {
                        replace: '替换',
                        insert: '右侧新增',
                        delete: '左侧独有',
                        unresolved: '尚未对齐',
                      } as RecordData
                    )[difference.tag] || difference.tag}
                  </span>
                  <button
                    className="text-button"
                    onClick={() =>
                      setExpanded((previous) => {
                        const next = new Set(previous);
                        if (next.has(index)) next.delete(index);
                        else next.add(index);
                        return next;
                      })
                    }
                  >
                    {full ? '收起完整差异' : '展开完整差异'}
                  </button>
                </div>
                <div className="diff-grid">
                  <div className="diff-before">
                    <span>
                      左侧 {left[0]}–{left[1]}
                    </span>
                    <p>
                      {sliceText(texts[0], left[0], left[1], full ? undefined : 300) || '（无）'}
                      {!full && left[1] - left[0] > 300 ? '…（预览前 300 字）' : ''}
                    </p>
                  </div>
                  <div className="diff-after">
                    <span>
                      右侧 {right[0]}–{right[1]}
                    </span>
                    <p>
                      {sliceText(texts[1], right[0], right[1], full ? undefined : 300) || '（无）'}
                      {!full && right[1] - right[0] > 300 ? '…（预览前 300 字）' : ''}
                    </p>
                  </div>
                </div>
              </article>
            );
          })}
          {!result.opcodes?.length && (
            <Empty title="两份来源的文字一致" icon="check">
              <p>可以选择其中一份作为底本，仍保留不同来源记录。</p>
            </Empty>
          )}
          <Pager
            page={pagination.page}
            total={result.opcodes?.length || 0}
            size={pagination.size}
            onChange={pagination.setPage}
          />
        </>
      )}
    </section>
  );
}
