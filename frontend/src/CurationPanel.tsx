import { useEffect, useMemo, useState } from 'react';
import { api, post } from './api';
import { useTargetRows } from './AssignmentTarget';
import { Issues, JsonDetails, Pager, shortId, usePagination } from './components';
import { usePlanJob } from './planJob';
import PlanHistory from './PlanHistory';
import { SourceResource } from './SourcePanel';
import { useWorkbench } from './store';
import { nameOf, textOf, type RecordData } from './types';

const planPath = '/api/curation-plans';
const rowLabels: Record<string, string> = {
  base: '底本',
  exact_duplicate: '内容一致，保留底本',
  conflict: '有差异，待选择',
  addition: '有可选补充',
  unplaced: '位置待核对',
};
const reasonLabels: Record<string, string> = {
  base_only: '保留底本已有内容',
  identical_content: '原文、结构与资源一致',
  text_variation: '原文有差异',
  split_or_merge: '段落拆分或合并',
  structure_variation: '结构或资源不同',
  competing_donors: '多份来源对同一位置有不同补充',
  anchored_insertion: '可以根据相同段落确定插入位置',
  no_reliable_placement: '没有可靠的插入位置',
  moved_match: '相同内容出现在不同位置',
  ambiguous_match: '对应关系待人工核对',
  overlapping_base_conflict: '插入点位于需要整体选择的底本区间内',
};

function PlanRanges({
  spans,
  editionLabel,
  sourceCache,
}: {
  spans: RecordData[];
  editionLabel: (id: string) => string;
  sourceCache: Map<string, RecordData>;
}) {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<RecordData[]>();
  const [error, setError] = useState('');
  useEffect(() => {
    if (!open || data) return;
    const controller = new AbortController();
    const sourceIds = [...new Set(spans.map((span) => span.source_id))];
    void Promise.all(
      sourceIds.map(async (id) => {
        const cached = sourceCache.get(id);
        if (cached) return cached;
        const source = await api(`/api/sources/${id}`, { signal: controller.signal });
        sourceCache.set(id, source);
        return source;
      }),
    )
      .then((sources) => {
        if (!controller.signal.aborted) setData(sources);
      })
      .catch((error) => {
        if (!controller.signal.aborted) setError(error.message);
      });
    return () => controller.abort();
  }, [data, open, sourceCache, spans]);

  return (
    <details onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary>查看原文区间与资源</summary>
      {spans.map((span) => {
        const source = data?.find((item) => item.id === span.source_id);
        const block = source?.blocks?.find((item: RecordData) => item.id === span.block_id);
        const text = block && Array.from(textOf(block)).slice(span.start, span.end).join('');
        return (
          <div
            className="alignment-ranges"
            key={`${span.source_id}:${span.block_id}:${span.start}:${span.end}`}
          >
            <small>
              {source ? nameOf(source) : `来源 ${shortId(span.source_id)}`} · 块{' '}
              {shortId(span.block_id)} · 字符 {span.start}–{span.end}
            </small>
            {!!span.edition_ranges?.length && (
              <p className="field-help">
                {span.edition_ranges
                  .map(
                    (range: RecordData) =>
                      `${editionLabel(range.edition_id)} · 字符 ${range.start}–${range.end}`,
                  )
                  .join('；')}
              </p>
            )}
            {block && (
              <>
                <pre>{text || '（无文字）'}</pre>
                {(block.resources || [])
                  .filter((resource: RecordData) => resource.sha256)
                  .map((resource: RecordData, index: number) => (
                    <SourceResource
                      key={`${resource.sha256}:${index}`}
                      sourceId={span.source_id}
                      resource={resource}
                    />
                  ))}
              </>
            )}
          </div>
        );
      })}
      {open && !data && !error && <p role="status">正在读取原文…</p>}
      {error && <Issues issues={[error]} />}
    </details>
  );
}

export default function CurationPanel({
  bookId,
  editionId,
  onOpen,
}: {
  bookId: string;
  editionId: string;
  onOpen: (revisionId: string, bookId: string) => void;
}) {
  const { state, run, busy, hasUnsavedEdits } = useWorkbench();
  const [query, setQuery] = useState('');
  const [base, setBase] = useState<RecordData>();
  const [donors, setDonors] = useState<RecordData[]>([]);
  const [allowComposite, setAllowComposite] = useState(false);
  const [jobId, setJobId] = useState('');
  const [pollAttempt, setPollAttempt] = useState(0);
  const [choices, setChoices] = useState<Record<string, string>>({});
  const [title, setTitle] = useState('');
  const [revisionId, setRevisionId] = useState('');
  const [historical, setHistorical] = useState<RecordData>();
  const job = usePlanJob(jobId, planPath, pollAttempt);
  const plan = historical || job.plan;
  const sourceCache = useMemo(() => new Map<string, RecordData>(), [plan?.id]);
  const pending = ['queued', 'running', 'paused'].includes(job.status);
  const sources = useTargetRows(
    'sources',
    `limit=25&book_id=${bookId}&edition_id=${editionId}&search=${encodeURIComponent(query.trim())}`,
    !!bookId && !!editionId,
    state.assignment_generation,
  );
  const compositeSources = useTargetRows(
    'sources',
    `limit=25&book_id=${bookId}&search=${encodeURIComponent(query.trim())}`,
    !!bookId && !!editionId && allowComposite,
    state.assignment_generation,
  );
  const donorSources = allowComposite ? compositeSources : sources;
  const rows = (plan?.rows || []) as RecordData[];
  const pagination = usePagination(rows, 8);
  const undecided = rows.filter((row) => !row.default_option_id && !choices[row.id]).length;
  const savedId = revisionId || plan?.result?.revision_id;
  const sourceLabel = (id: string) => {
    const item = [base, ...donors, ...sources.items, ...donorSources.items].find(
      (source) => source?.id === id,
    );
    return item ? nameOf(item) : plan?.source_names?.[id] || `来源 ${shortId(id)}`;
  };
  const editionLabel = (id: string) => plan?.editions?.[id]?.label || shortId(id);
  const selectionChanged =
    plan &&
    !historical &&
    (!!plan.allow_composite !== allowComposite ||
      plan.base_source_id !== base?.id ||
      [...plan.donor_source_ids].sort().join(',') !==
        donors
          .map((item) => item.id)
          .sort()
          .join(','));
  const start = async () => {
    if (!base) return;
    const response = await run('底本组稿计划已提交。', () =>
      post(`/api/books/${bookId}/curation-plans`, {
        edition_id: editionId,
        base_source_id: base.id,
        donor_source_ids: donors.map((item) => item.id),
        ...(allowComposite ? { allow_composite: true } : {}),
      }),
    );
    if (response) {
      setJobId(response.job?.id || response.job_id || response.id);
      setChoices({});
      setTitle('');
      setRevisionId('');
      setHistorical(undefined);
      pagination.setPage(0);
    }
  };
  const assemble = async () => {
    const result = await run('已生成整理稿。', () =>
      post(`${planPath}/${plan?.id}/assemble`, {
        snapshot_sha: plan?.snapshot_sha,
        choices,
        ...(title.trim() ? { title: title.trim() } : {}),
      }),
    );
    if (result?.revision_id) {
      setRevisionId(result.revision_id);
      onOpen(result.revision_id, plan?.book_id || bookId);
    }
  };

  return (
    <details className="card guided-panel">
      <summary>
        <strong>以底本组稿</strong>
      </summary>
      <p className="muted">
        选择目标版本的底本和补充来源，核对重复、补充位置与差异，再生成整理稿。
      </p>
      <PlanHistory
        kind="curation_plan"
        path={planPath}
        busy={busy > 0 || pending}
        onLoad={(loaded) => {
          setJobId('');
          setHistorical(loaded);
          setRevisionId('');
          setChoices({});
          setTitle('');
          pagination.setPage(0);
        }}
      />
      {!bookId || !editionId ? (
        <p>请先选择目标图书和版本，并确认来源归属。</p>
      ) : (
        <>
          <fieldset disabled={pending || busy > 0} className="guided-fields">
            <label className="checkbox-label">
              <input
                type="checkbox"
                checked={allowComposite}
                onChange={(event) => {
                  setAllowComposite(event.target.checked);
                  setDonors([]);
                }}
              />
              允许同一本书不同版本参与整理，保留版本区别
            </label>
            <label>
              搜索已归属来源
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="文件名或标题"
              />
            </label>
            <label>
              底本来源
              <select
                aria-label="底本来源"
                value={base?.id || ''}
                onChange={(event) => {
                  const item = sources.items.find((source) => source.id === event.target.value);
                  setBase(item);
                  setDonors((current) => current.filter((source) => source.id !== item?.id));
                }}
              >
                <option value="">选择底本</option>
                {base && !sources.items.some((source) => source.id === base.id) && (
                  <option value={base.id}>{nameOf(base)}</option>
                )}
                {sources.items.map((source) => (
                  <option key={source.id} value={source.id}>
                    {nameOf(source)}
                  </option>
                ))}
              </select>
            </label>
            <label>
              补充来源（最多四份）
              <select
                aria-label="补充来源（最多四份）"
                value=""
                disabled={donors.length >= 4}
                onChange={(event) => {
                  const item = donorSources.items.find(
                    (source) => source.id === event.target.value,
                  );
                  if (item) setDonors((current) => [...current, item]);
                }}
              >
                <option value="">添加补充来源</option>
                {donorSources.items
                  .filter(
                    (source) =>
                      source.id !== base?.id && !donors.some((item) => item.id === source.id),
                  )
                  .map((source) => (
                    <option key={source.id} value={source.id}>
                      {nameOf(source)}
                    </option>
                  ))}
              </select>
            </label>
            {donors.map((source) => (
              <button
                className="button small"
                key={source.id}
                onClick={() =>
                  setDonors((current) => current.filter((item) => item.id !== source.id))
                }
              >
                移除 {nameOf(source)}
              </button>
            ))}
            <button
              className="button primary"
              disabled={!base || !donors.length}
              onClick={() => void start()}
            >
              生成组稿计划
            </button>
          </fieldset>
          <Issues issues={[...new Set([sources.error, donorSources.error])].filter(Boolean)} />
        </>
      )}
      {pending && (
        <p role="status">
          {job.status === 'paused' ? '任务已暂停，可在任务中心恢复。' : '正在对照底本与补充来源…'}
        </p>
      )}
      {jobId && !pending && !plan && (
        <div className="guided-actions">
          <Issues issues={[job.error || '计划未完成，请在任务中心查看。']} />
          <button className="button small" onClick={() => setPollAttempt((value) => value + 1)}>
            重新读取任务
          </button>
        </div>
      )}
      {plan && (
        <>
          <p>
            计划共 {rows.length} 项 · 待选择 {undecided} 项
          </p>
          <p className="muted">
            底本 {sourceLabel(plan.base_source_id)} · 补充来源{' '}
            {plan.donor_source_ids.map(sourceLabel).join('、')}
          </p>
          {plan.allow_composite && (
            <div className="inline-banner">
              <strong>跨版本综合整理</strong>
              <span>
                {[plan.base_source_id, ...plan.donor_source_ids]
                  .map(
                    (id) =>
                      `${sourceLabel(id)}（${(plan.source_editions?.[id] || []).map(editionLabel).join('、')}）`,
                  )
                  .join('；')}
              </span>
            </div>
          )}
          {selectionChanged && !savedId && (
            <Issues issues={['来源选择已变化，请重新生成组稿计划。']} />
          )}
          {plan.incomplete && (
            <Issues
              issues={[
                '本次对照达到处理上限。可减少补充来源后重新生成计划，或在来源对照中手工选段。',
              ]}
            />
          )}
          {pagination.visible.map((row) => (
            <article className="difference-card" key={row.id}>
              <div className="block-heading">
                <strong>{rowLabels[row.kind] || row.kind}</strong>
                <span>
                  {row.reason_codes
                    .map((reason: string) => reasonLabels[reason] || reason)
                    .join(' · ')}
                </span>
              </div>
              <fieldset
                className="guided-options"
                aria-label={rowLabels[row.kind] || row.kind}
                disabled={!!savedId || busy > 0}
              >
                {row.options.map((option: RecordData) => {
                  const donors = [
                    ...new Set<string>(
                      option.spans
                        .map((span: RecordData) => span.source_id)
                        .filter((id: string) => id !== plan.base_source_id),
                    ),
                  ];
                  const label = option.spans.length
                    ? donors.length
                      ? `采用 ${donors.map(sourceLabel).join('、')}`
                      : '保留底本'
                    : '本稿暂不采用';
                  return (
                    <div className="alignment-ranges" key={option.id}>
                      {row.default_option_id ? (
                        <strong>{label}</strong>
                      ) : (
                        <label className="checkbox-label">
                          <input
                            type="radio"
                            name={`curation-${row.id}`}
                            checked={choices[row.id] === option.id}
                            onChange={() =>
                              setChoices((current) => ({ ...current, [row.id]: option.id }))
                            }
                          />
                          {label}
                        </label>
                      )}
                      {option.preview && <pre>{option.preview}</pre>}
                      {!!option.spans.length && (
                        <PlanRanges
                          spans={option.spans}
                          editionLabel={editionLabel}
                          sourceCache={sourceCache}
                        />
                      )}
                    </div>
                  );
                })}
              </fieldset>
              {row.evidence && <JsonDetails title="查看位置与匹配依据" value={row.evidence} />}
            </article>
          ))}
          <Pager
            page={pagination.page}
            total={rows.length}
            size={pagination.size}
            onChange={pagination.setPage}
          />
          {savedId ? (
            <button
              className="button primary"
              disabled={hasUnsavedEdits}
              onClick={() => onOpen(savedId, plan.book_id)}
            >
              打开整理稿
            </button>
          ) : (
            <div className="guided-fields">
              <label>
                整理稿标题（可选）
                <input value={title} onChange={(event) => setTitle(event.target.value)} />
              </label>
              <button
                className="button primary"
                disabled={
                  busy > 0 ||
                  pending ||
                  hasUnsavedEdits ||
                  undecided > 0 ||
                  !rows.length ||
                  plan.incomplete ||
                  selectionChanged
                }
                onClick={() => void assemble()}
              >
                确认生成整理稿
              </button>
            </div>
          )}
        </>
      )}
    </details>
  );
}
