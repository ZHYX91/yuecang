import { useEffect, useRef, useState } from 'react';
import { api, post } from './api';
import { Issues, JsonDetails, Pager, shortId, usePagination } from './components';
import { useWorkbench } from './store';
import {
  sourceSpansToSelections,
  textOf,
  type RecordData,
  type SelectedSpan,
  type Source,
  type Block,
} from './types';

const decisionLabels: Record<string, string> = {
  pending: '待核查',
  reviewed: '已核查',
  ignored: '已忽略',
};
const opinionLabels: Record<string, string> = {
  possible_mixture: '可能混入其他作品',
  same_work: '可能属于同一作品',
  uncertain: '依据不足',
};

function saveJson(value: unknown, filename: string) {
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(value, null, 2)], { type: 'application/json' }),
  );
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export default function SourceInspection({
  source,
  blocks,
  add,
}: {
  source: Source;
  blocks: Block[];
  add?: (spans: SelectedSpan[]) => void;
}) {
  const { state, run, busy, notify } = useWorkbench();
  const [report, setReport] = useState<RecordData | null>(null);
  const [analysisLoading, setAnalysisLoading] = useState(false);
  const [analysisError, setAnalysisError] = useState('');
  const [review, setReview] = useState<RecordData | null>(null);
  const [reviews, setReviews] = useState<RecordData[]>([]);
  const [reviewError, setReviewError] = useState('');
  const [responseText, setResponseText] = useState('');
  const [readingResponse, setReadingResponse] = useState(false);
  const [selectedBoundaries, setSelectedBoundaries] = useState<string[]>([]);
  const assignmentVersion = state.assignments?.[0]?.id;
  const reportVersion = useRef(assignmentVersion);
  const staleReport = report && reportVersion.current !== assignmentVersion;
  const boundaries = usePagination<RecordData>(report?.boundaries || [], 10);
  const matches = usePagination<RecordData>(report?.matches || [], 10);
  const acceptReport = (result: RecordData) => {
    reportVersion.current = assignmentVersion;
    setReport(result);
    setSelectedBoundaries(
      (result.boundaries || [])
        .filter((item: RecordData) => item.state !== 'ignored')
        .slice(0, 20)
        .map((item: RecordData) => item.id),
    );
    boundaries.setPage(0);
    matches.setPage(0);
  };
  useEffect(() => {
    const controller = new AbortController();
    setAnalysisLoading(true);
    setAnalysisError('');
    void api(`/api/sources/${source.id}/inspect`, {
      method: 'POST',
      body: '{}',
      signal: controller.signal,
    })
      .then((result) => {
        if (!controller.signal.aborted) acceptReport(result);
      })
      .catch((error) => {
        if (error.name !== 'AbortError') setAnalysisError(error.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setAnalysisLoading(false);
      });
    return () => controller.abort();
  }, [source.id]);
  useEffect(() => {
    const controller = new AbortController();
    void api(`/api/sources/${source.id}/boundary-reviews`, { signal: controller.signal })
      .then((result) => {
        if (controller.signal.aborted) return;
        setReviews((previous) => [
          ...previous,
          ...(result.items || []).filter(
            (item: RecordData) => !previous.some((existing) => existing.id === item.id),
          ),
        ]);
      })
      .catch((error) => {
        if (error.name !== 'AbortError') setReviewError(error.message);
      });
    return () => controller.abort();
  }, [source.id]);
  const inspect = async () => {
    setReport(null);
    const result = await run('', () => post(`/api/sources/${source.id}/inspect`));
    if (result) {
      acceptReport(result);
      setAnalysisError('');
    }
  };
  const decide = async (boundaryId: string, decision: string) => {
    const result = await run('边界核查状态已保存。', () =>
      post(`/api/sources/${source.id}/boundaries/${boundaryId}/decision`, { state: decision }),
    );
    if (result) {
      setReport((previous) =>
        previous
          ? {
              ...previous,
              boundaries: previous.boundaries.map((item: RecordData) =>
                item.id === boundaryId ? { ...item, state: decision } : item,
              ),
            }
          : null,
      );
      if (decision === 'ignored')
        setSelectedBoundaries((previous) => previous.filter((id) => id !== boundaryId));
    }
  };
  const makeReview = async () => {
    const boundaryIds = selectedBoundaries;
    const result = await run('离线核查请求已生成，模型意见仍需人工核对。', () =>
      post(`/api/sources/${source.id}/boundary-reviews`, { boundary_ids: boundaryIds }),
    );
    if (result) {
      setReview(result);
      setReviews((previous) => [result, ...previous.filter((item) => item.id !== result.id)]);
      setResponseText('');
    }
  };
  const receive = async () => {
    if (!review) return;
    let response: RecordData;
    try {
      response = JSON.parse(responseText);
    } catch {
      notify('核查结果不是有效 JSON，请检查后重新导入。', 'error');
      return;
    }
    const result = await run('核查意见已保存，来源归属尚未改变。', () =>
      post(`/api/boundary-reviews/${review.id}/receive`, response),
    );
    if (result) {
      const completed = { ...review, ...result };
      setReview(completed);
      setReviews((previous) =>
        previous.map((item) => (item.id === completed.id ? completed : item)),
      );
      setResponseText('');
    }
  };
  const chooseSpans = (spans: RecordData[]) => {
    try {
      add?.(sourceSpansToSelections(spans, [{ source, blocks }]));
    } catch (error) {
      notify(error instanceof Error ? error.message : '无法选择来源范围。', 'error');
    }
  };
  const pendingCount = (report?.boundaries || []).filter(
    (item: RecordData) => item.state === 'pending',
  ).length;
  return (
    <>
      {!staleReport && report?.boundaries?.length > 0 && (
        <div className="inline-banner warm source-inspection-alert" role="status">
          可能含不同图书：{report?.boundaries.length} 处线索，{pendingCount}{' '}
          处待核查。也可能是引用或分卷，请结合原文判断。
        </div>
      )}
      {!staleReport && report?.incomplete && (
        <Issues issues={['来源检查未覆盖全部内容，以下线索并非完整结果；其余范围仍需核对。']} />
      )}
      <Issues issues={analysisError ? [analysisError] : []} />
      <details className="source-tools">
        <summary>
          混书线索与已知图书匹配
          {analysisLoading && ' · 正在本地检查…'}
          {staleReport && ' · 需重新检查'}
          {report && !staleReport && ` · ${pendingCount} 处待核查`}
        </summary>
        <p className="muted">
          章节跳转或标题变化只是线索。核查记录和模型意见都不会自动拆书、归属或补写内容。
        </p>
        <div className="button-group">
          <button
            className="button small"
            disabled={busy > 0 || analysisLoading}
            onClick={() => void inspect()}
          >
            检查来源线索
          </button>
        </div>
        {staleReport && <p className="field-help">图书片段归属已更新，请重新检查来源线索。</p>}
        {report && !staleReport && (
          <>
            <h3>疑似边界 · {report.boundaries?.length || 0}</h3>
            <p className="field-help">
              可勾选需要模型核查的线索，每次最多 20 个；当前已选 {selectedBoundaries.length} 个。
            </p>
            {boundaries.visible.map((boundary: RecordData, index: number) => (
              <article className="candidate-preview" key={boundary.id}>
                <div className="block-heading">
                  <label className="checkbox-label">
                    <input
                      type="checkbox"
                      aria-label={`核查线索 ${boundaries.page * boundaries.size + index + 1}`}
                      checked={selectedBoundaries.includes(boundary.id)}
                      disabled={busy > 0 || boundary.state === 'ignored'}
                      onChange={(event) => {
                        const checked = event.target.checked;
                        if (checked && selectedBoundaries.length >= 20) {
                          notify('每次最多核查 20 个边界，请先取消一项或分批核查。', 'info');
                          return;
                        }
                        setSelectedBoundaries((previous) =>
                          checked
                            ? [...previous, boundary.id]
                            : previous.filter((id) => id !== boundary.id),
                        );
                      }}
                    />
                    <strong>
                      线索 {boundaries.page * boundaries.size + index + 1} ·{' '}
                      {decisionLabels[boundary.state] || boundary.state}
                    </strong>
                  </label>
                  <span>
                    块 {shortId(boundary.position?.block_id)} · 字符 {boundary.position?.start}–
                    {boundary.position?.end}
                  </span>
                </div>
                <div className="diff-grid">
                  <div>
                    <small>边界之前</small>
                    <pre>{boundary.before}</pre>
                  </div>
                  <div>
                    <small>边界之后</small>
                    <pre>{boundary.after}</pre>
                  </div>
                </div>
                <p className="field-help">
                  {(boundary.reasons || []).join('；')}
                  {boundary.uncertainty ? ` · ${boundary.uncertainty}` : ''}
                </p>
                {boundary.evidence?.length > 0 && (
                  <JsonDetails title="查看边界的局部来源证据" value={boundary.evidence} />
                )}
                <div className="button-group">
                  <button
                    className="button small"
                    disabled={busy > 0 || boundary.state === 'reviewed'}
                    onClick={() => void decide(boundary.id, 'reviewed')}
                  >
                    记为已核查
                  </button>
                  <button
                    className="button small"
                    disabled={busy > 0 || boundary.state === 'ignored'}
                    onClick={() => void decide(boundary.id, 'ignored')}
                  >
                    忽略此线索
                  </button>
                  {boundary.state !== 'pending' && (
                    <button
                      className="text-button"
                      disabled={busy > 0}
                      onClick={() => void decide(boundary.id, 'pending')}
                    >
                      恢复待核查
                    </button>
                  )}
                </div>
              </article>
            ))}
            <Pager
              page={boundaries.page}
              total={report.boundaries?.length || 0}
              size={boundaries.size}
              onChange={boundaries.setPage}
            />
            {!report.boundaries?.length && (
              <p className="field-help">未发现明显边界线索，这不等于已证明整份来源属于同一本书。</p>
            )}
            <h3>已知图书匹配 · {report.matches?.length || 0}</h3>
            {matches.visible.map((match: RecordData) => {
              const { block_id, start, end } = match.span;
              const block = blocks.find((item) => item.id === block_id);
              const text = block && Array.from(textOf(block)).slice(start, end).join('');
              return (
                <article className="candidate-preview" key={match.id}>
                  <strong>
                    {state.books.find((book) => book.id === match.book_id)?.title || '已知图书'} ·{' '}
                    {state.editions.find((edition) => edition.id === match.edition_id)?.label ||
                      '待确认版本'}
                  </strong>
                  <p className="field-help">
                    块 {shortId(block_id)} · 字符 {start}–{end}
                  </p>
                  <pre>{text || match.preview}</pre>
                  <p className="field-help">
                    {match.uncertainty || '局部文本相似，归属仍需核实。'}
                  </p>
                  <JsonDetails title="查看匹配证据与原始区间" value={match.evidence} />
                  {add && (
                    <button
                      className="button small"
                      disabled={busy > 0 || !text}
                      onClick={() => chooseSpans([match.span])}
                    >
                      选入整理片段
                    </button>
                  )}
                </article>
              );
            })}
            <Pager
              page={matches.page}
              total={report.matches?.length || 0}
              size={matches.size}
              onChange={matches.setPage}
            />
            <p className="field-help">
              尚未匹配 {report.unmatched?.length || 0} 个范围；未匹配不表示需要删除。
            </p>
            <div className="button-group">
              <button
                className="button small"
                disabled={busy > 0 || readingResponse || !selectedBoundaries.length}
                onClick={() => void makeReview()}
              >
                生成边界离线核查请求
              </button>
            </div>
          </>
        )}
        <Issues issues={reviewError ? [reviewError] : []} />
        {reviews.length > 0 && (
          <label>
            已保存的边界核查请求
            <select
              aria-label="已保存的边界核查请求"
              disabled={busy > 0 || readingResponse}
              value={review?.id || ''}
              onChange={(event) => {
                setReview(reviews.find((item) => item.id === event.target.value) || null);
                setResponseText('');
              }}
            >
              <option value="">选择请求，继续离线核查</option>
              {reviews.map((item) => (
                <option key={item.id} value={item.id}>
                  {shortId(item.id)} · {item.status === 'completed' ? '已导入意见' : '等待结果'}
                </option>
              ))}
            </select>
          </label>
        )}
        {review && (
          <section className="boundary-review">
            <div className="card-heading">
              <h3>边界离线核查</h3>
              <button
                className="button small"
                onClick={() =>
                  saveJson(review.request, `yuecang-boundaries-${shortId(review.id)}.json`)
                }
              >
                下载边界核查 JSON
              </button>
            </div>
            <p className="field-help">
              请求 {shortId(review.id)} ·{' '}
              {review.status === 'completed' ? '意见已导入' : '等待核查结果'}
              。将下载内容交给模型，再导入原请求对应的结果。
            </p>
            <JsonDetails title="查看核查请求" value={review.request} />
            {(review.opinions || []).map((opinion: RecordData) => (
              <div className="candidate-preview" key={opinion.boundary_id}>
                <strong>
                  {opinionLabels[opinion.verdict] || opinion.verdict} ·{' '}
                  {shortId(opinion.boundary_id)}
                </strong>
                <p>{opinion.reason}</p>
              </div>
            ))}
            {review.status !== 'completed' && (
              <>
                <label>
                  边界核查结果
                  <textarea
                    className="code-input"
                    aria-label="边界核查结果"
                    disabled={busy > 0 || readingResponse}
                    rows={6}
                    value={responseText}
                    onChange={(event) => setResponseText(event.target.value)}
                    placeholder={
                      '{"request_id":"请求 ID","opinions":[{"boundary_id":"边界 ID","verdict":"uncertain","reason":"依据不足"}]}'
                    }
                  />
                </label>
                <div className="button-group">
                  <button
                    className="button small accent"
                    disabled={busy > 0 || readingResponse || !responseText.trim()}
                    onClick={() => void receive()}
                  >
                    校验并保存核查意见
                  </button>
                  <label className="button small file-button">
                    读取核查结果文件
                    <input
                      type="file"
                      accept=".json,application/json"
                      disabled={busy > 0 || readingResponse}
                      onChange={async (event) => {
                        const file = event.target.files?.[0];
                        event.currentTarget.value = '';
                        if (!file) return;
                        if (file.size > 1024 * 1024) {
                          notify('核查结果超过 1 MiB，请检查是否选错文件。', 'error');
                          return;
                        }
                        setReadingResponse(true);
                        try {
                          setResponseText(await file.text());
                        } catch {
                          notify('无法读取核查结果文件，请重试。', 'error');
                        } finally {
                          setReadingResponse(false);
                        }
                      }}
                    />
                  </label>
                </div>
              </>
            )}
          </section>
        )}
      </details>
    </>
  );
}
