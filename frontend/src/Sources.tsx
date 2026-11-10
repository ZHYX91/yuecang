import { useEffect, useRef, useState } from 'react';
import { api, post } from './api';
import {
  Badge,
  Empty,
  Icon,
  JsonDetails,
  Issues,
  Pager,
  Search,
  SectionHeading,
  bytes,
} from './components';
import { useCollection } from './collections';
import { SourcePanel } from './SourcePanel';
import OrganizationPanel from './OrganizationPanel';
import { useWorkbench } from './store';
import {
  derivationOf,
  encodingOptions,
  fileNameOf,
  nameOf,
  type RecordData,
  type SelectedSpan,
} from './types';

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
  const { state, run, busy, notify, remember } = useWorkbench();
  const [query, setQuery] = useState('');
  const [organizing, setOrganizing] = useState(false);
  const [organizationIds, setOrganizationIds] = useState<string[]>([]);
  const [dragging, setDragging] = useState(false);
  const [analysis, setAnalysis] = useState<{ sourceId: string; result: RecordData } | null>(null);
  const [encoding, setEncoding] = useState('auto');
  const [converter, setConverter] = useState('auto');
  const [importOptions, setImportOptions] = useState<RecordData | null>(null);
  const [optionsError, setOptionsError] = useState('');
  const input = useRef<HTMLInputElement>(null);
  const preview = useRef<HTMLElement>(null);
  useEffect(() => {
    const controller = new AbortController();
    void api('/api/import-options', { signal: controller.signal })
      .then(setImportOptions)
      .catch((error) => {
        if (error.name !== 'AbortError') setOptionsError(error.message);
      });
    return () => controller.abort();
  }, []);
  const pagination = useCollection('sources', { search: query.trim() }, 15);
  const candidates = (state.candidates || []).filter(
    (item: RecordData) =>
      item.source_id === selected &&
      item.other_source_id &&
      ['exact_text', 'related_content'].includes(item.kind),
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
    form.append('encoding', encoding);
    form.append('doc_converter', converter);
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
          <p>TXT、Markdown、EPUB、DOC、DOCX、PDF、图片 · 原件与处理结果分别保存</p>
        </div>
        <span className="local-chip">原件保留在本地</span>
      </div>
      <details className="import-options">
        <summary>导入选项 · 文本编码与旧版 DOC 转换</summary>
        <div className="option-grid">
          <label>
            TXT / Markdown 编码
            <select
              aria-label="导入文本编码"
              value={encoding}
              onChange={(event) => setEncoding(event.target.value)}
              disabled={busy > 0}
            >
              {(importOptions?.encodings || encodingOptions).map((option: RecordData) => (
                <option key={option.id} value={option.id}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            DOC 转换器
            <select
              aria-label="DOC 转换器"
              value={converter}
              onChange={(event) => setConverter(event.target.value)}
              disabled={busy > 0}
            >
              {(
                importOptions?.converters || [
                  { id: 'auto', label: '自动选择已安装的转换器', available: true },
                ]
              ).map((option: RecordData) => (
                <option
                  key={option.id}
                  value={option.id}
                  disabled={!option.available && option.id !== 'auto'}
                >
                  {option.label}
                  {!option.available && option.id !== 'auto' ? '（不可用）' : ''}
                </option>
              ))}
            </select>
          </label>
        </div>
        <p className="field-help">
          这些选项仅应用于本批相应格式。DOC 需要本机已安装的转换器；不会自动下载。
        </p>
        {(importOptions?.converters || [])
          .filter((option: RecordData) => !option.available && option.reason)
          .map((option: RecordData) => (
            <p className="field-help" key={option.id}>
              {option.label}：{option.reason}
            </p>
          ))}
        <Issues issues={optionsError ? [optionsError] : []} />
      </details>
      <p className="format-note">
        Markdown 配图需要作为关联资源提供，缺失时会明确标记；扫描 PDF 的文字识别需要本机 OCR 工具。
      </p>
      <div className="sources-layout">
        <section className="card source-list-card">
          <div className="card-heading">
            <h2>
              来源文件{' '}
              <span className="count">{state.totals?.sources ?? state.sources.length}</span>
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
          <div className="guided-actions">
            <button
              className="button small"
              aria-expanded={organizing}
              onClick={() => setOrganizing(!organizing)}
            >
              批量归属{organizationIds.length ? `（已选 ${organizationIds.length} 份）` : ''}
            </button>
            {organizing && organizationIds.length > 0 && (
              <button className="button small" onClick={() => setOrganizationIds([])}>
                清空选择
              </button>
            )}
          </div>
          {organizing && <p className="field-help">可跨页选择，单次最多检查 20 份来源。</p>}
          {pagination.error && <Issues issues={[pagination.error]} />}
          {pagination.loading && (
            <p role="status" className="field-help">
              正在读取来源…
            </p>
          )}
          {pagination.items.length ? (
            <div className="source-list">
              {pagination.items.map((source) => (
                <div className="guided-source-row" key={source.id}>
                  {organizing && (
                    <input
                      type="checkbox"
                      checked={organizationIds.includes(source.id)}
                      disabled={
                        organizationIds.length >= 20 && !organizationIds.includes(source.id)
                      }
                      aria-label={`选择归属检查来源 ${nameOf(source)}`}
                      onChange={(event) =>
                        setOrganizationIds((current) =>
                          event.target.checked
                            ? [...current, source.id]
                            : current.filter((id) => id !== source.id),
                        )
                      }
                    />
                  )}
                  <button
                    data-source-id={source.id}
                    className={`source-list-row ${selected === source.id ? 'active' : ''}`}
                    onClick={() => {
                      remember('sources', [source]);
                      setSelected(source.id);
                    }}
                  >
                    <span className="file-type">
                      {(source.extension || fileNameOf(source).split('.').pop() || 'FILE')
                        .replace('.', '')
                        .slice(0, 4)
                        .toUpperCase()}
                    </span>
                    <span className="source-row-main">
                      <strong title={nameOf(source)}>{nameOf(source)}</strong>
                      <small>
                        {derivationOf(source) ||
                          (source.byte_size !== undefined || source.size !== undefined
                            ? bytes(source.byte_size ?? source.size)
                            : '大小待统计')}{' '}
                        · {source.block_count ?? source.blocks_count ?? '—'} 个内容块
                      </small>
                    </span>
                    <Badge status={source.status} />
                  </button>
                </div>
              ))}
            </div>
          ) : !pagination.loading && !pagination.error ? (
            <Empty title={query ? '没有找到文件' : '来源库还是空的'} icon="sources">
              <p>{query ? '尝试更短的名称。' : '导入一份文件，开始建立你的书库。'}</p>
            </Empty>
          ) : null}
          <Pager
            page={pagination.page}
            total={pagination.total}
            size={pagination.size}
            onChange={pagination.setPage}
          />
        </section>
        <section className="card source-preview" ref={preview}>
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
          <SourcePanel
            key={selected}
            sourceId={selected}
            selections={selections}
            add={add}
            remove={remove}
          />
        </section>
      </div>
      <div hidden={!organizing}>
        <OrganizationPanel
          sourceIds={organizationIds}
          onOpenSource={(id, name) => {
            remember('sources', [{ id, name }]);
            setSelected(id);
            preview.current?.scrollIntoView({ block: 'start' });
          }}
        />
      </div>
    </>
  );
}
