import { useEffect, useRef, useState } from 'react';
import { api, post } from './api';
import { useWorkbench } from './store';
import { Badge, Empty, Issues, Pager, Search, usePagination } from './components';
import SourceRecovery from './SourceRecovery';
import SourceInspection from './SourceInspection';
import {
  codePointOffset,
  characterLength,
  fileNameOf,
  nameOf,
  spanKey,
  textOf,
  type Block,
  type SelectedSpan,
  type Source,
} from './types';

const blockLabels: Record<string, string> = {
  heading: '标题',
  paragraph: '正文',
  list: '列表',
  table: '表格',
  figure: '图片',
  image: '图片',
  formula: '公式',
  footnote: '脚注',
  code: '代码',
  page_break: '分页',
  unknown: '待识别',
};
export function SourcePanel({
  sourceId,
  selections = [],
  add,
  remove,
  compact = false,
}: {
  sourceId: string;
  selections?: SelectedSpan[];
  add?: (spans: SelectedSpan[]) => void;
  remove?: (key: string) => void;
  compact?: boolean;
}) {
  const { state, run, busy } = useWorkbench();
  const assignmentCount = (state.assignments || []).filter(
    (item: any) => item.source_id === sourceId,
  ).length;
  const [reloadKey, setReloadKey] = useState(0);
  const [source, setSource] = useState<Source | null>(null);
  const [blocks, setBlocks] = useState<Block[]>([]);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(Boolean(sourceId));
  const [query, setQuery] = useState('');
  const [range, setRange] = useState<{ block: Block; start: number; end: number } | null>(null);
  const textareas = useRef<Record<string, HTMLTextAreaElement | null>>({});
  useEffect(() => {
    setError('');
    if (!sourceId) return;
    const controller = new AbortController();
    void api<any>(`/api/sources/${encodeURIComponent(sourceId)}`, { signal: controller.signal })
      .then((data) => {
        if (controller.signal.aborted) return;
        setSource(data.source || data);
        setBlocks(data.blocks || data.source?.blocks || data.extraction?.blocks || []);
      })
      .catch((error) => {
        if (error.name !== 'AbortError') setError(error.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [sourceId, reloadKey, assignmentCount]);
  const filtered = blocks.filter(
    (block) => !query || textOf(block).toLowerCase().includes(query.toLowerCase()),
  );
  const pagination = usePagination(filtered, compact ? 10 : 15);
  const toSpan = (block: Block, start = 0, end?: number): SelectedSpan => {
    const text = textOf(block);
    return {
      source_id: sourceId,
      block_id: block.id,
      start,
      end: end ?? characterLength(text),
      text: end === undefined ? text : Array.from(text).slice(start, end).join(''),
      source_name: source ? nameOf(source) : sourceId,
      type: block.type,
    };
  };
  if (!sourceId)
    return (
      <Empty title="选择一份来源" icon="sources">
        <p>在上方选择文件，查看原文与结构。</p>
      </Empty>
    );
  if (loading)
    return (
      <div className="loading-inline">
        <span className="spinner" />
        正在读取来源…
      </div>
    );
  if (error && !source) return <Issues issues={[error]} />;
  return (
    <div className={`source-panel ${compact ? 'compact' : ''}`}>
      <Issues issues={error ? [error] : []} />
      {source && (
        <div className="source-panel-meta">
          <div>
            <strong title={nameOf(source)}>{nameOf(source)}</strong>
            <span>{blocks.length} 个内容块</span>
          </div>
          <Badge status={source.status || 'ready'} />
          {source.asset_sha && (
            <a
              className="button small"
              href={`/api/sources/${sourceId}/resources/${source.asset_sha}`}
              download
            >
              下载原件
            </a>
          )}
        </div>
      )}
      <Issues issues={source?.issues} />
      {!compact &&
        source &&
        (['txt', 'markdown'].includes(source.metadata?.source_format) ||
          /\.(txt|md|markdown)$/i.test(fileNameOf(source))) && (
          <SourceRecovery key={sourceId} sourceId={sourceId} />
        )}
      {!compact && source && (
        <SourceInspection key={sourceId} source={source} blocks={blocks} add={add} />
      )}
      {add && source && source.metadata?.suggested_segments?.length > 0 && (
        <details className="segment-suggestions">
          <summary>识别到 {source.metadata.suggested_segments.length} 个范围候选</summary>
          <p className="muted">候选不会自动归并。确认范围后，可选入整理片段。</p>
          {source.metadata.suggested_segments.map((segment: any, index: number) => (
            <div className="segment-row" key={index}>
              <span>
                <strong>{segment.title || '待确认范围'}</strong>
                <small>
                  {segment.block_ids?.length || 0} 个内容块 · {segment.reason || '需确认归属'}
                </small>
              </span>
              <button
                className="button small"
                onClick={() =>
                  add(
                    blocks
                      .filter((block) => segment.block_ids?.includes(block.id))
                      .map((block) => toSpan(block)),
                  )
                }
              >
                选择此范围
              </button>
            </div>
          ))}
        </details>
      )}
      <div className="source-toolbar">
        <Search
          value={query}
          onChange={(value) => {
            setQuery(value);
            pagination.setPage(0);
          }}
          placeholder="搜索来源内容"
        />
        {add && (
          <button
            className="button small"
            disabled={!pagination.visible.length}
            onClick={() => add(pagination.visible.map((block) => toSpan(block)))}
          >
            选择本页
          </button>
        )}
      </div>
      {range && add && (
        <div className="range-action">
          <span>已选 {range.end - range.start} 字</span>
          <button
            className="button small accent"
            onClick={() => {
              add([toSpan(range.block, range.start, range.end)]);
              setRange(null);
            }}
          >
            采用所选文字
          </button>
        </div>
      )}
      <div className="source-blocks">
        {pagination.visible.map((block, index) => {
          const full = toSpan(block);
          const checked = selections.some((span) => spanKey(span) === spanKey(full));
          return (
            <article className={`source-block ${checked ? 'is-selected' : ''}`} key={block.id}>
              <div className="block-heading">
                <label>
                  {add && (
                    <input
                      type="checkbox"
                      aria-label={`选择第 ${pagination.page * pagination.size + index + 1} 块`}
                      checked={checked}
                      onChange={(event) =>
                        event.target.checked ? add([full]) : remove?.(spanKey(full))
                      }
                    />
                  )}
                  <span className="block-number">
                    {String(pagination.page * pagination.size + index + 1).padStart(2, '0')}
                  </span>
                  <span>{blockLabels[block.type] || block.type || '正文'}</span>
                </label>
                {block.locator?.page && <span>第 {block.locator.page} 页</span>}
              </div>
              {!textOf(block).trim() && textOf(block).length > 0 ? (
                <p className="blank-block">空白 / 分隔 · {textOf(block).length} 个字符</p>
              ) : textOf(block) ? (
                <textarea
                  ref={(element) => {
                    textareas.current[block.id] = element;
                  }}
                  className={`source-text type-${block.type}`}
                  aria-label={`来源内容 ${pagination.page * pagination.size + index + 1}`}
                  value={textOf(block)}
                  readOnly
                  rows={Math.max(
                    2,
                    Math.min(10, Math.ceil(textOf(block).length / (compact ? 35 : 60))),
                  )}
                  onSelect={(event) => {
                    const element = event.currentTarget;
                    if (element.selectionEnd > element.selectionStart)
                      setRange({
                        block,
                        start: codePointOffset(textOf(block), element.selectionStart),
                        end: codePointOffset(textOf(block), element.selectionEnd),
                      });
                  }}
                />
              ) : (
                <p className="muted resource-placeholder">
                  此块为{blockLabels[block.type] || '资源'}，原始结构会随所选片段保留。
                </p>
              )}
              {block.issues && <Issues issues={block.issues} />}
              {(block.resources || [])
                .filter((resource: any) => resource.sha256)
                .map((resource: any, ordinal: number) => (
                  <SourceResource
                    key={`${resource.sha256}:${ordinal}`}
                    sourceId={sourceId}
                    resource={resource}
                  />
                ))}
              <div className="disposition-row">
                <span>
                  {(source?.dispositions || [])
                    .filter((item: any) => item.block_id === block.id)
                    .map(
                      (item: any) =>
                        (
                          ({
                            used: '已归属',
                            pending: '待处理',
                            ignored: '明确忽略',
                            attachment: '附属内容',
                            duplicate: '重复内容',
                          }) as Record<string, string>
                        )[item.kind] || item.kind,
                    )
                    .filter(
                      (value: string, index: number, array: string[]) =>
                        array.indexOf(value) === index,
                    )
                    .join(' · ')}
                </span>
                <select
                  aria-label={`来源块 ${pagination.page * pagination.size + index + 1} 处置`}
                  value=""
                  disabled={busy > 0}
                  onChange={async (event) => {
                    const kind = event.target.value;
                    if (!kind) return;
                    const result = await run('来源块处置已记录。', () =>
                      post('/api/dispositions', {
                        source_id: sourceId,
                        block_id: block.id,
                        start: 0,
                        end: Array.from(textOf(block)).length,
                        kind,
                        reason: '用户在来源预览中明确选择',
                      }),
                    );
                    if (result) setReloadKey((value) => value + 1);
                  }}
                >
                  <option value="">标记此块…</option>
                  <option value="ignored">明确忽略</option>
                  <option value="attachment">保留为附属内容</option>
                  <option value="duplicate">标记重复内容</option>
                  <option value="pending">恢复待处理</option>
                </select>
              </div>
            </article>
          );
        })}
        {!filtered.length && (
          <Empty title={query ? '没有匹配的内容' : '暂无可用内容'}>
            <p>{query ? '尝试其他关键词。' : '检查提取状态或来源问题。'}</p>
          </Empty>
        )}
      </div>
      <Pager
        page={pagination.page}
        total={filtered.length}
        size={pagination.size}
        onChange={pagination.setPage}
      />
    </div>
  );
}

export function SourceResource({ sourceId, resource }: { sourceId: string; resource: any }) {
  const [open, setOpen] = useState(false);
  const url = `/api/sources/${encodeURIComponent(sourceId)}/resources/${encodeURIComponent(resource.sha256)}`;
  const raster = resource.mime
    ? ['image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/bmp'].includes(resource.mime)
    : /\.(png|jpe?g|gif|webp|bmp)$/i.test(resource.name || '');
  return (
    <div className="source-resource">
      <div className="button-group">
        {raster && (
          <button className="text-button" onClick={() => setOpen(!open)}>
            {open ? '收起原图' : '核对原图'}
          </button>
        )}
        <a href={url} download>
          {resource.name || '下载来源资源'}
        </a>
      </div>
      {raster && open && (
        <img src={url} alt={resource.name || '来源原图，用于核对提取内容'} loading="lazy" />
      )}
    </div>
  );
}
