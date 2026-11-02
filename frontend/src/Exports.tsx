import { useEffect, useState } from 'react';
import { post } from './api';
import {
  Badge,
  CollectionCoverage,
  Empty,
  Icon,
  Issues,
  JsonDetails,
  Pager,
  SectionHeading,
  date,
  shortId,
  usePagination,
} from './components';
import { useWorkbench } from './store';

export default function Exports({
  preferredRevision,
  openRevision,
}: {
  preferredRevision: string;
  openRevision: (id: string) => void;
}) {
  const { state, run, busy } = useWorkbench();
  const [revisionId, setRevisionId] = useState(preferredRevision);
  const [format, setFormat] = useState('markdown');
  const [allowDraft, setAllowDraft] = useState(false);
  useEffect(() => {
    if (!revisionId && preferredRevision) setRevisionId(preferredRevision);
  }, [preferredRevision, revisionId]);
  const revision = state.revisions.find((item) => item.id === revisionId);
  const isDraft = revision && !['final', 'finalized', 'completed'].includes(revision.status);
  const pagination = usePagination(state.exports, 15);
  return (
    <>
      <SectionHeading
        eyebrow="EXPORT LIBRARY"
        title="把整理成果带走"
        description="导出绑定明确的整理稿修订。原始来源、人工修改和历史版本仍保存在项目中。"
      />
      <section className="card export-form">
        <div className="export-illustration">
          <Icon name="exports" size={36} />
          <span>READY TO READ</span>
        </div>
        <div className="export-options">
          <label>
            整理稿
            <select
              aria-label="导出整理稿"
              value={revisionId}
              onChange={(event) => {
                setRevisionId(event.target.value);
                setAllowDraft(false);
              }}
            >
              <option value="">选择要导出的修订</option>
              {state.revisions.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.metadata?.title ||
                    state.books.find((book) => book.id === item.book_id)?.title ||
                    '整理稿'}{' '}
                  · {shortId(item.id)} ·{' '}
                  {['final', 'finalized'].includes(item.status) ? '已定版' : '草稿'}
                </option>
              ))}
            </select>
          </label>
          <div className="format-choices">
            {[
              ['markdown', 'Markdown', '正文与资源，便于继续编辑'],
              ['epub', 'EPUB', '带目录与元信息的电子书'],
              ['txt', '纯文本', '兼容性优先，明确记录格式降级'],
            ].map(([value, label, description]) => (
              <button
                key={value}
                className={`format-choice ${format === value ? 'active' : ''}`}
                onClick={() => setFormat(value)}
              >
                <strong>{label}</strong>
                <span>{description}</span>
              </button>
            ))}
          </div>
          {isDraft && (
            <label className="checkbox-label">
              <input
                type="checkbox"
                checked={allowDraft}
                onChange={(event) => setAllowDraft(event.target.checked)}
              />
              导出带问题说明的草稿；此操作不会将其定版。
            </label>
          )}
          <div className="button-group">
            <button
              className="button primary"
              disabled={!revisionId || (isDraft && !allowDraft) || busy > 0}
              onClick={() =>
                void run('导出已提交，完成后可从下方下载。', () =>
                  post('/api/exports', {
                    revision_id: revisionId,
                    format,
                    allow_draft: !!allowDraft,
                  }),
                )
              }
            >
              <Icon name="exports" size={17} />
              生成导出
            </button>
            {revisionId && (
              <button className="button" onClick={() => openRevision(revisionId)}>
                查看整理稿
              </button>
            )}
          </div>
        </div>
      </section>
      <div className="card-heading standalone">
        <h2>
          导出记录 <span className="count">{state.exports.length}</span>
        </h2>
      </div>
      <CollectionCoverage collection="exports" />
      <div className="export-list">
        {pagination.visible.map((item) => (
          <article key={item.id} className="card export-record">
            <div className="export-record-main">
              <span className="file-type">{(item.format || 'FILE').toUpperCase()}</span>
              <div>
                <h3>
                  {item.title ||
                    state.revisions.find((rev) => rev.id === item.revision_id)?.metadata?.title ||
                    '整理稿导出'}
                </h3>
                <p className="muted">
                  修订 {shortId(item.revision_id)} · {date(item.created_at)}
                </p>
              </div>
              <Badge status={item.status || 'succeeded'} />
              {(!item.status || ['succeeded', 'completed', 'ready'].includes(item.status)) && (
                <a
                  className="button small"
                  href={`/api/exports/${encodeURIComponent(item.id)}/download`}
                  download
                >
                  下载文件 <Icon name="exports" size={15} />
                </a>
              )}
            </div>
            <Issues
              issues={item.issues || item.warnings || item.report?.issues || item.report?.warnings}
            />
            {item.path && <p className="export-path">{item.path}</p>}
            <JsonDetails title="查看导出清单" value={item.manifest || item.report || item} />
          </article>
        ))}
      </div>
      {!state.exports.length && (
        <Empty title="还没有导出记录" icon="exports">
          <p>选择整理稿和格式，生成第一份整理成果。</p>
        </Empty>
      )}
      <Pager
        page={pagination.page}
        total={state.exports.length}
        size={pagination.size}
        onChange={pagination.setPage}
      />
    </>
  );
}
