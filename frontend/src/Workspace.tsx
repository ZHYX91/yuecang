import { useEffect, useState } from 'react';
import { post } from './api';
import { Empty, Icon, SectionHeading, shortId } from './components';
import { SourcePanel } from './SourcePanel';
import RevisionEditor from './RevisionEditor';
import Comparison from './Comparison';
import SourceAlignment from './SourceAlignment';
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
  const { state, run, busy, hasUnsavedEdits } = useWorkbench();
  const [leftId, setLeftId] = useState(sourceId);
  const [rightId, setRightId] = useState('');
  const [editionId, setEditionId] = useState('');
  const [allowComposite, setAllowComposite] = useState(false);
  const [tab, setTab] = useState<'compare' | 'edit'>(revisionId ? 'edit' : 'compare');
  const editions = state.editions.filter((item) => item.book_id === bookId);
  const revisions = state.revisions.filter((item) => item.book_id === bookId);
  const latestAlignmentAssignment = (state.assignments || []).find(
    (item: RecordData) =>
      item.book_id === bookId &&
      item.edition_id === editionId &&
      (item.source_id === leftId || item.source_id === rightId),
  );
  useEffect(() => {
    if (!leftId && state.sources.length) setLeftId(sourceId || state.sources[0].id);
  }, [leftId, state.sources, sourceId]);
  useEffect(() => {
    setEditionId('');
  }, [bookId]);
  useEffect(() => {
    setAllowComposite(false);
  }, [bookId, editionId]);
  useEffect(() => {
    if (revisionId) setTab('edit');
  }, [revisionId]);
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
  const sourceSelect = (value: string, change: (value: string) => void, label: string) => (
    <label className="source-selector">
      <span>{label}</span>
      <select aria-label={label} value={value} onChange={(event) => change(event.target.value)}>
        <option value="">选择来源文件</option>
        {state.sources.map((source) => (
          <option key={source.id} value={source.id}>
            {nameOf(source)}
          </option>
        ))}
      </select>
    </label>
  );
  return (
    <>
      <SectionHeading
        eyebrow="EDITORIAL WORKSPACE"
        title="让每一处取舍有据可依"
        description="对照来源，选择内容与顺序，保存为独立的整理稿。"
      />
      <div className="workspace-toolbar card">
        <label>
          目标图书
          <select
            aria-label="目标图书"
            disabled={hasUnsavedEdits}
            value={bookId}
            onChange={(event) => {
              setBookId(event.target.value);
              setRevisionId('');
            }}
          >
            <option value="">选择图书</option>
            {state.books.map((book) => (
              <option key={book.id} value={book.id}>
                {book.title}
              </option>
            ))}
          </select>
        </label>
        <label>
          目标版本
          <select
            aria-label="目标版本"
            value={editionId}
            onChange={(event) => setEditionId(event.target.value)}
          >
            <option value="">版本待确认</option>
            {editions.map((edition) => (
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
            整理稿 {revisions.length > 0 && <span>{revisions.length}</span>}
          </button>
        </div>
      </div>
      <div hidden={tab !== 'compare'} className="comparison-layout">
        <section className="card comparison-column">
          {sourceSelect(leftId, setLeftId, '左侧来源')}
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
          {sourceSelect(rightId, setRightId, '右侧来源')}
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
          key={JSON.stringify([bookId, editionId, leftId, rightId, latestAlignmentAssignment?.id])}
          bookId={bookId}
          editionId={editionId}
          leftId={leftId}
          rightId={rightId}
          add={add}
        />
        <Comparison leftId={leftId} rightId={rightId} />
      </div>
      <section hidden={tab !== 'edit'} className="card editor-card">
        {!revisionId && revisions.length > 0 && (
          <div className="editor-pick">
            <label>
              打开整理稿
              <select value="" onChange={(event) => setRevisionId(event.target.value)}>
                <option value="">选择已有修订</option>
                {revisions.map((revision) => (
                  <option key={revision.id} value={revision.id}>
                    {shortId(revision.id)} · {revision.metadata?.title || '整理稿'}
                  </option>
                ))}
              </select>
            </label>
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
