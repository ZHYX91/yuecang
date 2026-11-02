import { useEffect, useState } from 'react';
import { api, post } from './api';
import {
  Badge,
  CollectionCoverage,
  Empty,
  Issues,
  JsonDetails,
  Pager,
  SectionHeading,
  shortId,
  usePagination,
} from './components';
import { useWorkbench } from './store';
import type { RecordData } from './types';

export default function Review({
  requestId,
  setRequestId,
  openRevision,
}: {
  requestId: string;
  setRequestId: (id: string) => void;
  openRevision: (id: string) => void;
}) {
  const { state, run, busy, notify } = useWorkbench();
  const [request, setRequest] = useState<RecordData | null>(null);
  const [responseText, setResponseText] = useState('');
  const [requestError, setRequestError] = useState('');
  const [filter, setFilter] = useState('pending');
  const [showRequest, setShowRequest] = useState(!!requestId);
  useEffect(() => {
    if (!requestId) {
      setRequest(null);
      return;
    }
    setRequest(null);
    setResponseText('');
    setRequestError('');
    setShowRequest(true);
    const controller = new AbortController();
    void api(`/api/review-requests/${requestId}`, { signal: controller.signal })
      .then((data) => setRequest(data))
      .catch((error) => {
        if (error.name !== 'AbortError') setRequestError(error.message);
      });
    return () => controller.abort();
  }, [requestId]);
  const pending = (proposal: RecordData) =>
    !proposal.status || ['pending', 'proposed', 'waiting_review'].includes(proposal.status);
  const filtered = state.proposals.filter(
    (item) => filter === 'all' || (filter === 'pending' ? pending(item) : item.status === filter),
  );
  const pagination = usePagination(filtered, 12);
  const submitResponse = async () => {
    let response: unknown;
    try {
      response = JSON.parse(responseText);
    } catch {
      notify('响应不是有效的 JSON，请检查引号、逗号和括号。', 'error');
      return;
    }
    const result = await run('响应已校验并转为校订提案。', () =>
      post(`/api/review-requests/${requestId}/response`, { response }),
    );
    if (result) setResponseText('');
  };
  const downloadRequest = () => {
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(request, null, 2)], { type: 'application/json' }),
    );
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = `yuecang-review-${shortId(requestId)}.json`;
    anchor.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  return (
    <>
      <SectionHeading
        eyebrow="REVIEW DESK"
        title="每一次校订，都看得清楚"
        description="查看原文、建议和依据。接受建议会生成新修订，原始文本始终保留。"
        actions={
          <button className="button" onClick={() => setShowRequest(!showRequest)}>
            {showRequest ? '收起审阅请求' : '离线审阅请求'}
          </button>
        }
      />
      {showRequest && (
        <section className="card offline-review">
          <div className="card-heading">
            <h2>离线审阅</h2>
            {request && (
              <button className="button small" onClick={downloadRequest}>
                下载请求 JSON
              </button>
            )}
          </div>
          <p className="muted">
            在整理稿中创建离线审阅请求，将模型或人工处理结果导入这里。所有范围与证据都会再次校验。
          </p>
          <label>
            审阅请求
            <select
              aria-label="审阅请求"
              value={requestId}
              onChange={(event) => setRequestId(event.target.value)}
            >
              <option value="">选择审阅请求</option>
              {(state.review_requests || []).map((item) => (
                <option key={item.id} value={item.id}>
                  {state.revisions.find((r) => r.id === item.revision_id)?.metadata?.title ||
                    '整理稿'}
                  {' · '}
                  {shortId(item.revision_id)} / {shortId(item.id)} ·{' '}
                  {(
                    {
                      ready: '待处理',
                      completed: '已完成',
                      sending: '发送中',
                      needs_attention: '需要关注',
                    } as RecordData
                  )[item.status] ||
                    item.status ||
                    '待处理'}
                </option>
              ))}
              {requestId &&
                !(state.review_requests || []).some((item) => item.id === requestId) && (
                  <option value={requestId}>{shortId(requestId)}</option>
                )}
            </select>
          </label>
          <Issues issues={requestError ? [requestError] : []} />
          {request && (
            <>
              <div className="inline-banner warm review-batch">
                <div>
                  <strong>
                    共 {request.payload?.batch?.count || 1} 片 · 当前第{' '}
                    {request.payload?.batch?.index || 1} 片
                  </strong>
                  <p>
                    每片分别下载并导入结果。完成本片不代表全书完成；请在上方切换其他待处理请求。
                  </p>
                </div>
                <Badge
                  status={
                    state.review_requests?.find((r) => r.id === requestId)?.status || request.status
                  }
                />
              </div>
              <details className="provenance-details">
                <summary>本片范围（原始块的 Unicode 码点）</summary>
                {(request.payload?.blocks || []).map((block: RecordData) => (
                  <p key={`${block.id}:${block.start || 0}`}>
                    块 {shortId(block.id)}：{block.start || 0}–
                    {block.end ?? Array.from(block.text || '').length}
                    {' / '}
                    {block.block_length ?? Array.from(block.text || '').length} 字符
                  </p>
                ))}
              </details>
              <JsonDetails title="查看请求文本与允许引用的证据" value={request} />
              <label>
                导入结构化响应
                <textarea
                  aria-label="导入结构化响应"
                  className="code-input"
                  rows={8}
                  value={responseText}
                  onChange={(event) => setResponseText(event.target.value)}
                  placeholder={
                    '{"corrections": [{"block_id": "请求中的块 ID", "start": 0, "end": 1, "old_text": "原字", "new_text": "新字", "reason": "原因", "evidence": []}]}'
                  }
                />
              </label>
              <p className="field-help">
                corrections 包含 block_id、start/end（Unicode 码点范围）、old_text、new_text、reason
                和
                evidence。坐标从原始块开始计数，不能从分片重新归零，也不能越片；证据只能引用本请求提供的来源区间。
              </p>
              <div className="button-group">
                <button
                  className="button primary"
                  disabled={busy > 0 || !responseText.trim()}
                  onClick={() => void submitResponse()}
                >
                  校验并导入提案
                </button>
                <label className="button file-button">
                  从 JSON 文件读取
                  <input
                    type="file"
                    accept=".json,application/json"
                    onChange={async (event) => {
                      const file = event.target.files?.[0];
                      if (file) setResponseText(await file.text());
                      event.target.value = '';
                    }}
                  />
                </label>
              </div>
            </>
          )}
        </section>
      )}
      <div className="list-toolbar">
        <div className="filter-tabs">
          {[
            ['pending', '待审阅'],
            ['accepted', '已接受'],
            ['rejected', '已拒绝'],
            ['all', '全部'],
          ].map(([value, label]) => (
            <button
              key={value}
              className={filter === value ? 'active' : ''}
              onClick={() => {
                setFilter(value);
                pagination.setPage(0);
              }}
            >
              {label}
              {value === 'pending' && <span>{state.proposals.filter(pending).length}</span>}
            </button>
          ))}
        </div>
        <span className="muted">{filtered.length} 条校订</span>
      </div>
      <CollectionCoverage collection="proposals" />
      <div className="proposal-list">
        {pagination.visible.map((proposal) => {
          const revision = state.revisions.find(
            (item) => item.id === (proposal.revision_id || proposal.base_revision_id),
          );
          const book = state.books.find(
            (item) => item.id === (proposal.book_id || revision?.book_id),
          );
          const detail =
            proposal.correction || proposal.operation || proposal.operations?.[0] || proposal;
          return (
            <article className="card proposal-card" key={proposal.id}>
              <div className="proposal-heading">
                <div>
                  <span className="eyebrow">
                    {book?.title || '整理稿校订'} ·{' '}
                    {proposal.category || proposal.kind || '文字校订'}
                  </span>
                  <h3>{detail.reason || proposal.reason || '检查并确认这处修改'}</h3>
                </div>
                <Badge status={proposal.status || 'pending'} />
              </div>
              <div className="diff-grid">
                <div className="diff-before">
                  <span>原文</span>
                  <p>
                    {detail.old_text ??
                      detail.before ??
                      proposal.original_text ??
                      '请查看关联修订中的原文范围'}
                  </p>
                </div>
                <div className="diff-after">
                  <span>建议</span>
                  <p>
                    {detail.new_text ?? detail.text ?? detail.replacement ?? detail.after ?? ''}
                  </p>
                </div>
              </div>
              <div className="proposal-footer">
                <div className="muted">
                  块 {shortId(detail.block_id)}
                  {detail.start !== undefined ? ` · 字符 ${detail.start}–${detail.end}` : ''}
                  {proposal.risk
                    ? ` · 风险：${({ high: '较高', normal: '一般', low: '较低' } as RecordData)[proposal.risk] || proposal.risk}`
                    : ''}
                </div>
                <div className="button-group">
                  {revision && (
                    <button className="button small" onClick={() => openRevision(revision.id)}>
                      查看整理稿
                    </button>
                  )}
                  {pending(proposal) && (
                    <>
                      <button
                        className="button small"
                        disabled={busy > 0}
                        onClick={() =>
                          void run('已拒绝这条建议。', () =>
                            post(`/api/proposals/${proposal.id}/decide`, { decision: 'reject' }),
                          )
                        }
                      >
                        拒绝
                      </button>
                      <button
                        className="button small primary"
                        disabled={busy > 0}
                        onClick={() =>
                          void run('建议已接受并生成新修订。', () =>
                            post(`/api/proposals/${proposal.id}/decide`, { decision: 'accept' }),
                          )
                        }
                      >
                        接受修改
                      </button>
                    </>
                  )}
                </div>
              </div>
              {(detail.evidence || proposal.evidence) && (
                <JsonDetails title="查看来源依据" value={detail.evidence || proposal.evidence} />
              )}
            </article>
          );
        })}
      </div>
      {!filtered.length && (
        <Empty
          title={filter === 'pending' ? '没有待审阅的校订' : '没有符合条件的记录'}
          icon="review"
        >
          <p>打开整理稿，创建AI或离线审阅请求，建议会出现在这里。</p>
        </Empty>
      )}
      <Pager
        page={pagination.page}
        total={filtered.length}
        size={pagination.size}
        onChange={pagination.setPage}
      />
    </>
  );
}
