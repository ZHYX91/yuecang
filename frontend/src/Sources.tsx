import { useRef, useState } from 'react';
import { api, post } from './api';
import {
  Badge,
  CollectionCoverage,
  Empty,
  Icon,
  JsonDetails,
  Pager,
  Search,
  SectionHeading,
  bytes,
  usePagination,
} from './components';
import { SourcePanel } from './SourcePanel';
import { useWorkbench } from './store';
import { nameOf, type RecordData, type SelectedSpan } from './types';

export default function Sources({
  selected,
  setSelected,
  selections,
  add,
  remove,
  openWorkspace,
}: {
  selected: string;
  setSelected: (id: string) => void;
  selections: SelectedSpan[];
  add: (spans: SelectedSpan[]) => void;
  remove: (key: string) => void;
  openWorkspace: () => void;
}) {
  const { state, run, busy, notify } = useWorkbench();
  const [query, setQuery] = useState('');
  const [dragging, setDragging] = useState(false);
  const [analysis, setAnalysis] = useState<{ sourceId: string; result: RecordData } | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const filtered = state.sources.filter((source) =>
    nameOf(source).toLowerCase().includes(query.toLowerCase()),
  );
  const pagination = usePagination(filtered, 15);
  const candidates = (state.candidates || []).filter(
    (item: RecordData) => item.source_id === selected,
  );
  const analyzeSelected = async () => {
    const result = await run('', () => post(`/api/sources/${selected}/analyze`));
    if (result) {
      setAnalysis({ sourceId: selected, result });
      notify(
        result.suggestions
          ? '来源分析已完成，请查看相关来源与范围建议。'
          : '分析任务已提交，结果将在任务中心更新。',
        'success',
      );
    }
  };
  const importFiles = async (files: FileList | File[] | null) => {
    if (!files?.length) return;
    const form = new FormData();
    Array.from(files).forEach((file) => form.append('files', file));
    await run(`${files.length} 个文件已提交，提取状态会自动更新。`, () =>
      api('/api/imports', { method: 'POST', body: form }),
    );
    if (input.current) input.current.value = '';
  };
  return (
    <>
      <SectionHeading
        eyebrow="SOURCE LIBRARY"
        title="从每一份来源开始"
        description="原件保留，结构提取。把散落的文件，整理成可信的图书。"
        actions={
          <button
            className="button primary"
            disabled={busy > 0}
            onClick={() => input.current?.click()}
          >
            <Icon name="plus" />
            导入文件
          </button>
        }
      />
      <input
        ref={input}
        type="file"
        multiple
        className="visually-hidden"
        aria-label="导入来源文件"
        onChange={(event) => void importFiles(event.target.files)}
      />
      <div
        className={`import-strip ${dragging ? 'dragging' : ''}`}
        onDragOver={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault();
          setDragging(false);
          void importFiles(event.dataTransfer.files);
        }}
      >
        <div className="import-icon">
          <Icon name="upload" size={25} />
        </div>
        <div>
          <strong>
            将文件拖到这里，或
            <button className="text-button" onClick={() => input.current?.click()}>
              选择文件
            </button>
          </strong>
          <p>TXT、Markdown、EPUB、DOCX、PDF · 每个文件都会保留明确的处理结果</p>
        </div>
        <span className="local-chip">原件保留在本地</span>
      </div>
      <p className="format-note">
        Markdown 配图需要作为关联资源提供，缺失时会明确标记；扫描 PDF 的文字识别需要本机 OCR 工具。
      </p>
      <CollectionCoverage collection="sources" />
      <div className="sources-layout">
        <section className="card source-list-card">
          <div className="card-heading">
            <h2>
              来源文件 <span className="count">{state.sources.length}</span>
            </h2>
            <Search
              value={query}
              onChange={(value) => {
                setQuery(value);
                pagination.setPage(0);
              }}
              placeholder="查找文件"
            />
          </div>
          {filtered.length ? (
            <div className="source-list">
              {pagination.visible.map((source) => (
                <button
                  key={source.id}
                  className={`source-list-row ${selected === source.id ? 'active' : ''}`}
                  onClick={() => setSelected(source.id)}
                >
                  <span className="file-type">
                    {(source.extension || nameOf(source).split('.').pop() || 'FILE')
                      .replace('.', '')
                      .slice(0, 4)
                      .toUpperCase()}
                  </span>
                  <span className="source-row-main">
                    <strong>{nameOf(source)}</strong>
                    <small>
                      {source.byte_size !== undefined || source.size !== undefined
                        ? bytes(source.byte_size ?? source.size)
                        : '大小待统计'}{' '}
                      · {source.block_count ?? source.blocks_count ?? '—'} 个内容块
                    </small>
                  </span>
                  <Badge status={source.status} />
                </button>
              ))}
            </div>
          ) : (
            <Empty title={query ? '没有找到文件' : '来源库还是空的'} icon="sources">
              <p>{query ? '尝试更短的名称。' : '导入一份文件，开始建立你的书库。'}</p>
            </Empty>
          )}
          <Pager
            page={pagination.page}
            total={filtered.length}
            size={pagination.size}
            onChange={pagination.setPage}
          />
        </section>
        <section className="card source-preview">
          <div className="card-heading">
            <h2>来源预览</h2>
            {selected && (
              <button
                className="button small"
                disabled={busy > 0}
                onClick={() => void analyzeSelected()}
              >
                分析候选
              </button>
            )}
            {selections.length > 0 && (
              <button className="button small accent" onClick={openWorkspace}>
                已选 {selections.length} 段，去整理 <Icon name="arrow" size={15} />
              </button>
            )}
          </div>
          {candidates.length > 0 && (
            <div className="related-candidates">
              <h3>相关来源候选</h3>
              <p className="muted">内容重叠只是线索，归属与版本仍由你确认。</p>
              {candidates.map((candidate: RecordData) => {
                const other = state.sources.find((item) => item.id === candidate.other_source_id);
                return (
                  <div className="segment-row" key={candidate.id}>
                    <span>
                      <strong>
                        {other ? nameOf(other) : candidate.evidence?.right_title || '相关来源'}
                      </strong>
                      <small>
                        {candidate.kind === 'exact_text' ? '原文文字一致' : '有共同文本片段'}
                      </small>
                    </span>
                    <button
                      className="button small"
                      onClick={() => setSelected(candidate.other_source_id)}
                    >
                      查看来源
                    </button>
                  </div>
                );
              })}
            </div>
          )}
          {analysis?.sourceId === selected && (
            <JsonDetails title="查看本次候选分析结果" value={analysis.result} />
          )}
          <SourcePanel sourceId={selected} selections={selections} add={add} remove={remove} />
        </section>
      </div>
    </>
  );
}
