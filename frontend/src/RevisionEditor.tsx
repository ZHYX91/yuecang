import { useEffect, useState } from 'react';
import { api, post } from './api';
import { Badge, Empty, Issues, Pager, Search, date, shortId, usePagination } from './components';
import { useCollection } from './collections';
import { useWorkbench } from './store';
import { characterLength, textOf, type RecordData, type Revision } from './types';
import RevisionAdvanced from './RevisionAdvanced';

export default function RevisionEditor({
  revisionId,
  selectRevision,
  onReviewRequest,
}: {
  revisionId: string;
  selectRevision: (id: string) => void;
  onReviewRequest: (id: string) => void;
}) {
  const { state, run, busy, notify, remember, setHasUnsavedEdits } = useWorkbench();
  const [loadedRevision, setRevision] = useState<Revision | null>(null);
  const revision = loadedRevision?.id === revisionId ? loadedRevision : null;
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [provider, setProvider] = useState('offline');
  const [instruction, setInstruction] = useState(
    '检查错字、OCR 错误和标点。保留原意，不补写缺失内容。',
  );
  const [showReview, setShowReview] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);
  const [branchChoice, setBranchChoice] = useState<{ id: string; revisionId: string } | null>(null);
  const [branchQuery, setBranchQuery] = useState('');
  const [historyQuery, setHistoryQuery] = useState('');
  const [branchDetail, setBranchDetail] = useState<{
    id: string;
    row?: RecordData;
    error?: string;
  } | null>(null);
  const [advancedDirty, setAdvancedDirty] = useState(false);
  useEffect(() => {
    setRevision(null);
    setEdits({});
    setError('');
    if (!revisionId) return;
    const controller = new AbortController();
    setLoading(true);
    void api<Revision>(`/api/revisions/${encodeURIComponent(revisionId)}`, {
      signal: controller.signal,
    })
      .then((data) => {
        if (controller.signal.aborted) return;
        const { blocks, changes, ...summary } = data;
        remember('revisions', [summary]);
        setRevision(data);
      })
      .catch((error) => {
        if (error.name !== 'AbortError') setError(error.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [revisionId, reloadKey, remember]);
  useEffect(() => {
    const handler = (event: BeforeUnloadEvent) => {
      if (Object.keys(edits).length || advancedDirty) {
        event.preventDefault();
        event.returnValue = '';
      }
    };
    window.addEventListener('beforeunload', handler);
    return () => window.removeEventListener('beforeunload', handler);
  }, [edits, advancedDirty]);
  const bookId = revision?.book_id;
  const branches = useCollection(
    'branches',
    { book_id: bookId, search: branchQuery.trim() },
    20,
    !!bookId,
  );
  const history = useCollection(
    'revisions',
    { book_id: bookId, search: historyQuery.trim() },
    20,
    !!bookId,
  );
  const branchId = branchChoice?.revisionId === revisionId ? branchChoice.id : revision?.branch_id;
  const pageBranch = branchId
    ? branches.items.find((item) => item.id === branchId)
    : branches.items.find((item) => item.head_revision_id === revisionId) ||
      (!branchQuery.trim() && branches.total === 1 ? branches.items[0] : undefined);
  const missingBranch = branchId && !pageBranch ? branchId : '';
  useEffect(() => {
    if (!missingBranch) return;
    const controller = new AbortController();
    void api(`/api/branches/${encodeURIComponent(missingBranch)}`, { signal: controller.signal })
      .then((row) => {
        if (!controller.signal.aborted) setBranchDetail({ id: missingBranch, row });
      })
      .catch((error) => {
        if (!controller.signal.aborted)
          setBranchDetail({
            id: missingBranch,
            error: error instanceof Error ? error.message : '无法读取整理分支。',
          });
      });
    return () => controller.abort();
  }, [missingBranch, state.event_id]);
  const detail = branchDetail?.id === branchId ? branchDetail : null;
  const branch = pageBranch || (detail?.row?.book_id === bookId ? detail?.row : undefined);
  const bookBranches = branch && !pageBranch ? [branch, ...branches.items] : branches.items;
  const selectHistory = (id: string) => {
    if (branch) setBranchChoice({ id: branch.id, revisionId: id });
    selectRevision(id);
  };
  const isHead = !!branch && branch.head_revision_id === revisionId;
  const blocks = revision?.blocks || [];
  const pagination = usePagination(blocks, 12);
  const dirty = Object.keys(edits).length;
  useEffect(() => {
    setHasUnsavedEdits(dirty > 0 || advancedDirty);
    return () => setHasUnsavedEdits(false);
  }, [dirty, advancedDirty, setHasUnsavedEdits]);
  const save = async () => {
    if (!revision || !branch) return;
    const operations = blocks
      .filter((block) => edits[block.id] !== undefined)
      .map((block) => ({
        block_id: block.id,
        start: 0,
        end: characterLength(textOf(block)),
        text: edits[block.id],
      }));
    const result = await run('修改已保存为新修订，原有版本仍可查看。', () =>
      post(`/api/branches/${branch.id}/edits`, { expected_revision_id: revisionId, operations }),
    );
    if (result) {
      setEdits({});
      const next = result.revision_id || result.revision?.id || result.id;
      if (next) selectRevision(next);
      else setReloadKey((value) => value + 1);
    }
  };
  const requestReview = async () => {
    const result = await run(
      provider === 'offline' ? '离线审阅请求已准备。' : '模型审阅任务已提交。',
      () =>
        post(`/api/revisions/${revisionId}/review`, {
          provider,
          instruction,
          branch_id: branch?.id,
        }),
    );
    if (result) {
      if (result.batch_count > 1)
        notify(`已准备 ${result.batch_count} 个审阅分片，请逐片处理；单次预算按每片分别计算。`);
      setShowReview(false);
      onReviewRequest(
        result.request_id || result.review_request_id || result.request?.id || result.id || '',
      );
    }
  };
  if (!revisionId)
    return (
      <Empty title="尚未打开整理稿">
        <p>从来源生成整理稿，或在上方选择已有修订。</p>
      </Empty>
    );
  if (loading)
    return (
      <div className="loading-inline">
        <span className="spinner" />
        正在打开整理稿…
      </div>
    );
  if (error) return <Issues issues={[error]} />;
  if (!revision) return null;
  return (
    <div className="revision-editor">
      <div className="editor-heading">
        <div>
          <div className="eyebrow">
            {branch?.name || '整理稿'} · 修订 {shortId(revision.id)}
          </div>
          <h2>
            {revision.metadata?.title ||
              state.books.find((book) => book.id === revision.book_id)?.title ||
              '未命名整理稿'}
          </h2>
          <p className="muted">
            {revision.metadata?.author || '作者待确认'} · {blocks.length} 个内容块 ·{' '}
            {date(revision.created_at ?? revision.created)}
          </p>
        </div>
        <Badge status={revision.status || 'draft'} />
      </div>
      <details className="source-tools">
        <summary>搜索与翻页 · 整理分支和修订</summary>
        <div className="option-grid">
          <div>
            <Search
              value={branchQuery}
              onChange={(value) => {
                setBranchQuery(value);
                branches.setPage(0);
              }}
              placeholder="搜索分支名称"
            />
            <Pager
              page={branches.page}
              total={branches.total}
              size={branches.size}
              onChange={branches.setPage}
            />
          </div>
          <div>
            <Search
              value={historyQuery}
              onChange={(value) => {
                setHistoryQuery(value);
                history.setPage(0);
              }}
              placeholder="按题名或编号搜索本书修订"
            />
            <Pager
              page={history.page}
              total={history.total}
              size={history.size}
              onChange={history.setPage}
            />
          </div>
        </div>
      </details>
      <Issues issues={[branches.error, history.error, detail?.error].filter(Boolean)} />
      <div className="editor-toolbar">
        <label>
          整理分支
          <select
            aria-label="整理分支"
            disabled={dirty > 0 || advancedDirty}
            value={branch?.id || ''}
            onChange={(event) => {
              const next = bookBranches.find((item) => item.id === event.target.value);
              if (!next) return;
              const id = isHead && next.head_revision_id ? next.head_revision_id : revisionId;
              setBranchChoice({ id: next.id, revisionId: id });
              if (id !== revisionId) selectRevision(id);
            }}
          >
            <option value="">选择目标分支</option>
            {bookBranches.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name || shortId(item.id)}
              </option>
            ))}
          </select>
        </label>
        <label>
          查看修订
          <select
            aria-label="查看修订"
            value={revisionId}
            disabled={dirty > 0 || advancedDirty}
            onChange={(event) => selectHistory(event.target.value)}
          >
            {history.items.map((item) => (
              <option key={item.id} value={item.id}>
                {shortId(item.id)} ·{' '}
                {['final', 'finalized'].includes(item.status) ? '已定版' : '草稿'}{' '}
                {branch?.head_revision_id === item.id ? '· 当前' : ''}
              </option>
            ))}
            {!history.items.some((item) => item.id === revisionId) && (
              <option value={revisionId}>{shortId(revisionId)}</option>
            )}
          </select>
        </label>
        <div className="button-group">
          <button
            className="button"
            disabled={busy > 0 || dirty > 0 || advancedDirty || !isHead}
            onClick={() => setShowReview(!showReview)}
          >
            AI 校订
          </button>
          <button
            className="button"
            disabled={busy > 0 || dirty > 0 || advancedDirty}
            onClick={async () => {
              const result = await run('整理稿已定版。', () =>
                post(`/api/revisions/${revisionId}/finalize`),
              );
              if (result) setReloadKey((value) => value + 1);
            }}
          >
            定版
          </button>
          <button
            className="button primary"
            disabled={!dirty || busy > 0 || !isHead || advancedDirty}
            onClick={() => void save()}
          >
            保存修改{dirty ? `（${dirty}）` : ''}
          </button>
        </div>
      </div>
      {!isHead && (
        <div className="inline-banner">
          <span>
            {branch ? '正在查看历史修订，内容为只读。' : '请选择整理分支，查看当前稿或恢复此修订。'}
          </span>
          <button
            className="button small"
            disabled={!branch}
            onClick={() => branch && selectHistory(branch.head_revision_id)}
          >
            查看当前稿
          </button>
          <button
            className="button small"
            disabled={busy > 0 || !branch}
            onClick={async () => {
              const result = await run('已切换分支当前修订。', () =>
                post(`/api/branches/${branch!.id}/select`, {
                  expected_revision_id: branch!.head_revision_id,
                  revision_id: revisionId,
                }),
              );
              if (result) setReloadKey((value) => value + 1);
            }}
          >
            恢复此修订
          </button>
        </div>
      )}
      {dirty > 0 && (
        <div className="inline-banner warm">
          <span>{dirty} 处修改尚未保存。保存前，修订切换和AI校订暂不可用。</span>
          <button
            className="text-button"
            onClick={() => {
              setEdits({});
              notify('已放弃本次未保存修改。');
            }}
          >
            放弃本次修改
          </button>
        </div>
      )}
      <Issues issues={revision.issues} />
      <RevisionAdvanced
        revision={revision}
        branch={branch}
        isHead={isHead}
        textDirty={dirty > 0}
        onDirtyChange={setAdvancedDirty}
        onUpdated={(result) => {
          if (result.branch_id)
            setBranchChoice({ id: result.branch_id, revisionId: result.revision_id || revisionId });
          if (result.revision_id && result.revision_id !== revisionId)
            selectRevision(result.revision_id);
          else setReloadKey((value) => value + 1);
        }}
      />
      {showReview && (
        <div className="review-request-form">
          <label>
            审阅方式
            <select
              aria-label="审阅方式"
              value={provider}
              onChange={(event) => setProvider(event.target.value)}
            >
              <option value="offline">离线审阅 · 导出请求后导入响应</option>
              <option value="http">已配置的模型服务</option>
            </select>
          </label>
          <label>
            审阅要求
            <textarea
              rows={3}
              value={instruction}
              onChange={(event) => setInstruction(event.target.value)}
            />
          </label>
          <p className="muted">
            {provider === 'http'
              ? `本次会向设置中的 ${state.settings.ai?.model || '模型服务'} 发送当前整理稿及允许的来源证据。修改以提案返回，接受后才写入新修订。`
              : '生成包含当前文本和证据范围的请求。处理后导入结构化响应，逐项审阅。'}{' '}
            长书自动拆成多个请求，每片分别计量和处理。
          </p>
          <div className="button-group">
            <button
              className="button primary"
              disabled={busy > 0 || !isHead}
              onClick={() => void requestReview()}
            >
              创建审阅请求
            </button>
            <button className="button" onClick={() => setShowReview(false)}>
              取消
            </button>
          </div>
        </div>
      )}
      <div className="editable-blocks">
        {pagination.visible.map((block, index) => (
          <article
            className={`edit-block ${edits[block.id] !== undefined ? 'dirty' : ''}`}
            key={block.id}
          >
            <div className="block-heading">
              <span>
                <span className="block-number">
                  {String(pagination.page * pagination.size + index + 1).padStart(2, '0')}
                </span>
                {block.type === 'heading'
                  ? '标题'
                  : block.type === 'paragraph'
                    ? '正文'
                    : block.type}
              </span>
              {edits[block.id] !== undefined && <span className="unsaved">未保存</span>}
            </div>
            <textarea
              aria-label={`编辑第 ${pagination.page * pagination.size + index + 1} 块`}
              readOnly={!isHead || advancedDirty}
              value={edits[block.id] ?? textOf(block)}
              rows={Math.max(
                2,
                Math.min(15, Math.ceil((edits[block.id] ?? textOf(block)).length / 65)),
              )}
              onChange={(event) => {
                const text = event.target.value;
                setEdits((previous) => {
                  const next = { ...previous };
                  if (text === textOf(block)) delete next[block.id];
                  else next[block.id] = text;
                  return next;
                });
              }}
            />
            <Provenance block={block} editions={revision.metadata?.source_editions} />
          </article>
        ))}
      </div>
      <Pager
        page={pagination.page}
        total={blocks.length}
        size={pagination.size}
        onChange={pagination.setPage}
      />
    </div>
  );
}
function Provenance({ block, editions }: { block: RecordData; editions?: RecordData }) {
  const edges =
    block.provenance ||
    block.sources ||
    block.source_spans ||
    (block.source_id
      ? [{ source_id: block.source_id, block_id: block.source_block_id || block.id }]
      : []);
  const list = Array.isArray(edges) ? edges : [edges];
  return (
    <div className="provenance">
      {list.length ? (
        list.map((edge: any, index: number) => (
          <span key={index} title={JSON.stringify(edge)}>
            {edge.kind === 'edit' || edge.operation === 'edit' || edge.type === 'manual'
              ? '人工编辑'
              : '来源'}{' '}
            ·{' '}
            {edge.source_name || shortId(edge.source_id || edge.source?.source_id || edge.block_id)}
            {(edge.start ?? edge.source?.start) !== undefined
              ? ` / ${edge.start ?? edge.source?.start}–${edge.end ?? edge.source?.end}`
              : ''}
          </span>
        ))
      ) : (
        <span>此内容的来源信息请查看修订记录</span>
      )}
      {block.source_edition_ranges?.length > 0 && (
        <details>
          <summary>选材时的来源版本</summary>
          {block.source_edition_ranges.map((range: RecordData) => (
            <p
              key={`${range.source_id}:${range.block_id}:${range.start}:${range.end}:${range.edition_id}`}
            >
              {editions?.[range.edition_id]?.label || shortId(range.edition_id)} · 来源{' '}
              {shortId(range.source_id)} · 块 {shortId(range.block_id)} · 字符 {range.start}–
              {range.end}
            </p>
          ))}
        </details>
      )}
    </div>
  );
}
