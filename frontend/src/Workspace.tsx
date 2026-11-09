import { useEffect, useRef, useState } from 'react';
import { post } from './api';
import { Empty, Icon, Issues, Pager, SectionHeading, shortId } from './components';
import { useTargetRows } from './AssignmentTarget';
import { useCollection } from './collections';
import { SourcePanel } from './SourcePanel';
import RevisionEditor from './RevisionEditor';
import Comparison from './Comparison';
import SourceAlignment from './SourceAlignment';
import CurationPanel from './CurationPanel';
import { useWorkbench } from './store';
import {
  assemblyPayload,
  nameOf,
  payloadSpan,
  spanKey,
  type RecordData,
  type SelectedSpan,
} from './types';

export default function Workspace({
  bookId,
  setBookId,
  sourceId,
  revisionId,
  setRevisionId,
  selections,
  add,
  remove,
  reorder,
  clear,
  openBooks,
  onReviewRequest,
}: {
  bookId: string;
  setBookId: (id: string) => void;
  sourceId: string;
  revisionId: string;
  setRevisionId: (id: string) => void;
  selections: SelectedSpan[];
  add: (spans: SelectedSpan[]) => void;
  remove: (key: string) => void;
  reorder: (index: number, delta: number) => void;
  clear: () => void;
  openBooks: () => void;
  onReviewRequest: (id: string) => void;
}) {
  const { state, run, busy, hasUnsavedEdits, remember, notify } = useWorkbench();
  const unsavedRef = useRef(hasUnsavedEdits);
  const [leftId, setLeftId] = useState(sourceId);
  const [rightId, setRightId] = useState('');
  const [editionId, setEditionId] = useState('');
  const [allowComposite, setAllowComposite] = useState(false);
  const [tab, setTab] = useState<'compare' | 'edit'>(revisionId ? 'edit' : 'compare');
  const [bookQuery, setBookQuery] = useState('');
  const [editionQuery, setEditionQuery] = useState('');
  const [leftQuery, setLeftQuery] = useState('');
  const [rightQuery, setRightQuery] = useState('');
  const books = useTargetRows(
    'books',
    `limit=30&search=${encodeURIComponent(bookQuery.trim())}`,
    true,
    state.event_id,
  );
  const editions = useTargetRows(
    'editions',
    `limit=30&book_id=${bookId}&search=${encodeURIComponent(editionQuery.trim())}`,
    !!bookId,
    state.event_id,
  );
  const leftSources = useTargetRows(
    'sources',
    `limit=30&search=${encodeURIComponent(leftQuery.trim())}`,
    true,
    state.event_id,
  );
  const rightSources = useTargetRows(
    'sources',
    `limit=30&search=${encodeURIComponent(rightQuery.trim())}`,
    true,
    state.event_id,
  );
  const revisions = useCollection('revisions', { book_id: bookId }, 20, !!bookId);
  useEffect(() => {
    if (!leftId && state.sources.length) setLeftId(sourceId || state.sources[0].id);
  }, [leftId, state.sources, sourceId]);
  useEffect(() => {
    if (sourceId) setLeftId(sourceId);
  }, [sourceId]);
  useEffect(() => {
    setEditionId('');
    setEditionQuery('');
  }, [bookId]);
  useEffect(() => {
    setAllowComposite(false);
  }, [bookId, editionId]);
  useEffect(() => {
    if (revisionId) setTab('edit');
  }, [revisionId]);
  useEffect(() => {
    unsavedRef.current = hasUnsavedEdits;
  }, [hasUnsavedEdits]);
  const openDraft = (id: string, draftBookId: string) => {
    if (unsavedRef.current) {
      notify('整理稿已保存。请先保存或放弃当前编辑，再打开生成的整理稿。');
      return;
    }
    setBookId(draftBookId);
    setRevisionId(id);
    setTab('edit');
  };
  const assemble = async () => {
    const result = await run('整理稿已生成，可以继续校订和编辑。', () =>
      post(`/api/books/${bookId}/assemble`, assemblyPayload(selections, editionId, allowComposite)),
    );
    if (result) {
      setAllowComposite(false);
      const id = result.revision_id || result.revision?.id || result.id;
      if (id) {
        setRevisionId(id);
        setTab('edit');
      }
    }
  };
  const keepSelected = (items: RecordData[], selected: RecordData | undefined) =>
    selected && !items.some((item) => item.id === selected.id) ? [selected, ...items] : items;
  const sourceSelect = (
    value: string,
    change: (value: string) => void,
    label: string,
    query: string,
    setQuery: (value: string) => void,
    rows: ReturnType<typeof useTargetRows>,
  ) => {
    const choices = keepSelected(
      rows.items,
      state.sources.find((item) => item.id === value),
    );
    return (
      <div className="source-selector">
        <label>
          搜索{label}
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="文件名或标题"
          />
        </label>
        <label>
          <span>{label}</span>
          <select
            aria-label={label}
            value={value}
            onChange={(event) => {
              const source = choices.find((item) => item.id === event.target.value);
              if (source) remember('sources', [source]);
              change(event.target.value);
            }}
          >
            <option value="">选择来源文件</option>
            {choices.map((source) => (
              <option key={source.id} value={source.id}>
                {nameOf(source)}
              </option>
            ))}
          </select>
        </label>
        {rows.error && <Issues issues={[rows.error]} />}
      </div>
    );
  };
  return (
    <>
      <SectionHeading
        eyebrow="EDITORIAL WORKSPACE"
        title="让每一处取舍有据可依"
        description="对照来源，选择内容与顺序，保存为独立的整理稿。"
      />
      <div className="workspace-toolbar card">
        <label>
          搜索图书
          <input
            value={bookQuery}
            onChange={(event) => setBookQuery(event.target.value)}
            placeholder="书名或作者"
          />
        </label>
        <label>
          目标图书
          <select
            aria-label="目标图书"
            disabled={hasUnsavedEdits}
            value={bookId}
            onChange={(event) => {
              const book = books.items.find((item) => item.id === event.target.value);
              if (book) remember('books', [book]);
              setBookId(event.target.value);
              setRevisionId('');
            }}
          >
            <option value="">选择图书</option>
            {keepSelected(
              books.items,
              state.books.find((item) => item.id === bookId),
            ).map((book) => (
              <option key={book.id} value={book.id}>
                {book.title}
              </option>
            ))}
          </select>
        </label>
        {bookId && (
          <label>
            搜索版本
            <input
              value={editionQuery}
              onChange={(event) => setEditionQuery(event.target.value)}
              placeholder="版本名称或译者"
            />
          </label>
        )}
        <label>
          目标版本
          <select
            aria-label="目标版本"
            value={editionId}
            disabled={!bookId}
            onChange={(event) => {
              const edition = editions.items.find((item) => item.id === event.target.value);
              if (edition) remember('editions', [edition]);
              setEditionId(event.target.value);
            }}
          >
            <option value="">版本待确认</option>
            {keepSelected(
              bookId ? editions.items : [],
              state.editions.find((item) => item.id === editionId && item.book_id === bookId),
            ).map((edition) => (
              <option key={edition.id} value={edition.id}>
                {edition.label || edition.name}
              </option>
            ))}
          </select>
        </label>
        <button className="button small" onClick={openBooks}>
          <Icon name="plus" size={16} />
          管理图书与版本
        </button>
        <div className="tab-switch">
          <button className={tab === 'compare' ? 'active' : ''} onClick={() => setTab('compare')}>
            来源对照
          </button>
          <button className={tab === 'edit' ? 'active' : ''} onClick={() => setTab('edit')}>
            整理稿 {!!bookId && revisions.total > 0 && <span>{revisions.total}</span>}
          </button>
        </div>
      </div>
      <Issues issues={[books.error, editions.error].filter(Boolean)} />
      <div hidden={tab !== 'compare'}>
        <CurationPanel
          key={JSON.stringify([bookId, editionId])}
          bookId={bookId}
          editionId={editionId}
          onOpen={openDraft}
        />
      </div>
      <div hidden={tab !== 'compare'} className="comparison-layout">
        <section className="card comparison-column">
          {sourceSelect(leftId, setLeftId, '左侧来源', leftQuery, setLeftQuery, leftSources)}
          <SourcePanel
            key={leftId}
            sourceId={leftId}
            compact
            selections={selections}
            add={add}
            remove={remove}
          />
        </section>
        <section className="card comparison-column">
          {sourceSelect(rightId, setRightId, '右侧来源', rightQuery, setRightQuery, rightSources)}
          <SourcePanel
            key={rightId}
            sourceId={rightId}
            compact
            selections={selections}
            add={add}
            remove={remove}
          />
        </section>
        <aside className="card selection-tray">
          <div className="card-heading">
            <h2>
              整理片段 <span className="count">{selections.length}</span>
            </h2>
            {selections.length > 0 && (
              <button className="text-button" onClick={clear}>
                清空
              </button>
            )}
          </div>
          <p className="muted">按下方顺序生成整理稿。可选择整个内容块，也可在原文中划选一段。</p>
          <div className="tray-items">
            {selections.map((span, index) => (
              <article key={spanKey(span)} className="tray-item">
                <div>
                  <span className="block-number">{String(index + 1).padStart(2, '0')}</span>
                  <strong title={span.source_name}>{span.source_name}</strong>
                </div>
                <p>
                  {span.text.slice(0, 130) || `[${span.type}]`}
                  {span.text.length > 130 ? '…' : ''}
                </p>
                {span.start !== undefined && (
                  <small>
                    选取字符 {span.start}–{span.end}
                  </small>
                )}
                <div className="tray-actions">
                  <button
                    aria-label={`片段 ${index + 1} 上移`}
                    disabled={index === 0}
                    onClick={() => reorder(index, -1)}
                  >
                    ↑
                  </button>
                  <button
                    aria-label={`片段 ${index + 1} 下移`}
                    disabled={index === selections.length - 1}
                    onClick={() => reorder(index, 1)}
                  >
                    ↓
                  </button>
                  <button
                    aria-label={`移除片段 ${index + 1}`}
                    onClick={() => remove(spanKey(span))}
                  >
                    移除
                  </button>
                </div>
              </article>
            ))}
          </div>
          {!selections.length && (
            <Empty title="选择要采用的内容" icon="workspace">
              <p>勾选左侧来源块，或划选具体文字。</p>
            </Empty>
          )}
          <div className="tray-footer">
            {!bookId && <p className="muted">先选择目标图书。</p>}
            <button
              className="button full-width"
              disabled={!bookId || !selections.length || busy > 0}
              onClick={() =>
                void run('所选片段已保存归属。', () =>
                  post('/api/assignments', {
                    book_id: bookId,
                    ...(editionId ? { edition_id: editionId } : {}),
                    spans: selections.map(payloadSpan),
                  }),
                )
              }
            >
              保存片段归属
            </button>
            <label className="checkbox-label">
              <input
                type="checkbox"
                checked={allowComposite}
                disabled={busy > 0 || hasUnsavedEdits}
                onChange={(event) => setAllowComposite(event.target.checked)}
              />
              确认为综合整理版，保留各版本出处
            </label>
            <button
              className="button primary full-width"
              disabled={!bookId || !selections.length || busy > 0 || hasUnsavedEdits}
              onClick={() => void assemble()}
            >
              生成整理稿 <Icon name="arrow" size={16} />
            </button>
          </div>
        </aside>
      </div>
      <div hidden={tab !== 'compare'}>
        <SourceAlignment
          key={JSON.stringify([bookId, editionId, leftId, rightId, state.assignment_generation])}
          bookId={bookId}
          editionId={editionId}
          leftId={leftId}
          rightId={rightId}
          add={add}
        />
        <Comparison leftId={leftId} rightId={rightId} />
      </div>
      <section hidden={tab !== 'edit'} className="card editor-card">
        {bookId && !revisionId && revisions.total > 0 && (
          <div className="editor-pick">
            <label>
              打开整理稿
              <select value="" onChange={(event) => setRevisionId(event.target.value)}>
                <option value="">选择已有修订</option>
                {revisions.items.map((revision) => (
                  <option key={revision.id} value={revision.id}>
                    {shortId(revision.id)} · {revision.metadata?.title || '整理稿'}
                  </option>
                ))}
              </select>
            </label>
            <Pager
              page={revisions.page}
              total={revisions.total}
              size={revisions.size}
              onChange={revisions.setPage}
            />
          </div>
        )}
        <RevisionEditor
          revisionId={revisionId}
          selectRevision={setRevisionId}
          onReviewRequest={onReviewRequest}
        />
      </section>
    </>
  );
}
