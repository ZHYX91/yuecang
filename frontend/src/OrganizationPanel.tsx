import { useState } from 'react';
import { post } from './api';
import { AssignmentTargetFields, type AssignmentTarget } from './AssignmentTarget';
import { Issues, JsonDetails, Pager, usePagination } from './components';
import { usePlanJob } from './planJob';
import PlanHistory from './PlanHistory';
import { useWorkbench } from './store';
import { type RecordData } from './types';

const planPath = '/api/assignment-plans';
const kinds: Record<string, string> = {
  matched: '已知图书匹配',
  ambiguous: '归属待核对',
  unassigned: '暂无匹配',
};
type Choice = {
  action?: 'confirm' | 'reject';
  target?: string;
  title?: string;
  author?: string;
  edition?: string;
  existing?: AssignmentTarget;
  groupNew?: boolean;
};

export default function OrganizationPanel({
  sourceIds,
  onOpenSource,
}: {
  sourceIds: string[];
  onOpenSource: (id: string, name: string) => void;
}) {
  const { run, busy, state } = useWorkbench();
  const [jobId, setJobId] = useState('');
  const [pollAttempt, setPollAttempt] = useState(0);
  const [choices, setChoices] = useState<Record<string, Choice>>({});
  const [result, setResult] = useState<RecordData>();
  const [historical, setHistorical] = useState<RecordData>();
  const job = usePlanJob(jobId, planPath, pollAttempt);
  const plan = historical || job.plan;
  const rows = (plan?.sources || []).flatMap((source: RecordData) =>
    source.candidates.map((candidate: RecordData) => ({ source, candidate })),
  ) as { source: RecordData; candidate: RecordData }[];
  const pagination = usePagination(rows, 8);
  const stale =
    plan?.state === 'ready' &&
    !result &&
    (plan.assignment_generation !== state.assignment_generation ||
      plan.disposition_generation !== state.disposition_generation);
  const update = (id: string, patch: Choice) =>
    setChoices((previous) => ({ ...previous, [id]: { ...previous[id], ...patch } }));
  const start = async () => {
    const response = await run('已提交批量归属检查。', () =>
      post(planPath, { source_ids: sourceIds }),
    );
    if (response) {
      setJobId(response.job?.id || response.job_id || response.id);
      setChoices({});
      setResult(undefined);
      setHistorical(undefined);
      pagination.setPage(0);
    }
  };
  const decisions = rows.flatMap(({ candidate }) => {
    const choice = choices[candidate.id];
    return choice?.action ? [{ candidate, choice }] : [];
  });
  const targetOf = (candidate: RecordData, choice: Choice) => {
    if (choice.target === 'existing')
      return choice.existing?.book_id && choice.existing.edition_id ? choice.existing : null;
    if (choice.target === 'new') {
      const title = choice.title?.trim();
      if (!title) return null;
      const edition = choice.edition?.trim() || '';
      const author = choice.author?.trim() || '';
      const signature = JSON.stringify([title, author, edition]);
      const first =
        choice.groupNew &&
        decisions.find(
          (item) =>
            item.choice.action === 'confirm' &&
            item.choice.target === 'new' &&
            item.choice.groupNew &&
            JSON.stringify([
              item.choice.title?.trim(),
              item.choice.author?.trim() || '',
              item.choice.edition?.trim() || '',
            ]) === signature,
        );
      return {
        new_book: {
          key: (first && first.candidate.id) || candidate.id,
          title,
          author,
          ...(edition ? { edition } : {}),
        },
      };
    }
    if (choice.target) {
      const alternative = candidate.alternatives[Number(choice.target)];
      return alternative
        ? { book_id: alternative.book_id, edition_id: alternative.edition_id }
        : null;
    }
    return candidate.suggested_target || null;
  };
  const apply = async () => {
    const response = await run('已保存本次归属决定。', () =>
      post(`${planPath}/${plan?.id}/apply`, {
        decisions: decisions.map(({ candidate, choice }) => ({
          candidate_id: candidate.id,
          action: choice.action,
          ...(choice.action === 'confirm' ? { target: targetOf(candidate, choice) } : {}),
        })),
      }),
    );
    if (response) setResult(response);
  };
  const saved = result || plan?.result;
  const pending = ['queued', 'running', 'paused'].includes(job.status);
  return (
    <section className="card guided-panel">
      <div className="card-heading">
        <div>
          <h2>批量检查归属</h2>
          <p className="muted">
            已选 {sourceIds.length} 份来源。先核对原文和候选图书，再确认各区间的归属。
          </p>
        </div>
        <button
          className="button primary"
          disabled={!sourceIds.length || pending || busy > 0}
          onClick={() => void start()}
        >
          检查所选来源
        </button>
      </div>
      <PlanHistory
        kind="assignment_plan"
        path={planPath}
        busy={busy > 0 || pending}
        onLoad={(loaded) => {
          setJobId('');
          setHistorical(loaded);
          setResult(undefined);
          setChoices({});
          pagination.setPage(0);
        }}
      />
      {pending && (
        <p role="status">
          {job.status === 'paused' ? '检查已暂停，可在任务中心恢复。' : '正在检查所选来源…'}
        </p>
      )}
      {jobId && !pending && !plan && (
        <div className="guided-actions">
          <Issues issues={[job.error || '检查未完成，请在任务中心查看。']} />
          <button className="button small" onClick={() => setPollAttempt((value) => value + 1)}>
            重新读取任务
          </button>
        </div>
      )}
      {plan && (
        <>
          <p>
            本次检查 {plan.sources.length} 份来源 · {rows.length} 条区间建议
          </p>
          {!rows.length && <p className="muted">这批来源暂无待确认的区间建议。</p>}
          {plan.sources
            .filter((source: RecordData) => source.incomplete)
            .map((source: RecordData) => (
              <Issues
                key={source.source_id}
                issues={[`${source.name} 的部分范围未覆盖，请打开来源核对。`]}
              />
            ))}
          {stale && <Issues issues={['书库归属或区间处置已变化，请重新检查。']} />}
          {saved && (
            <p role="status">
              已确认 {saved.confirmed} 项，已拒绝 {saved.rejected} 项，待处理 {saved.deferred} 项。
            </p>
          )}
          {pagination.visible.map(({ source, candidate }) => {
            const choice = choices[candidate.id] || {};
            return (
              <article className="difference-card" key={candidate.id}>
                <div className="block-heading">
                  <strong>
                    {source.name} · {kinds[candidate.kind] || candidate.kind}
                  </strong>
                  <span>{candidate.title_hint}</span>
                </div>
                <button
                  className="button small"
                  onClick={() => onOpenSource(source.source_id, source.name)}
                >
                  在来源预览中打开
                </button>
                {candidate.spans.map((span: RecordData) => (
                  <div
                    className="alignment-ranges"
                    key={`${span.block_id}:${span.start}:${span.end}`}
                  >
                    <small>
                      块 {span.block_id.slice(0, 8)} · 字符 {span.start}–{span.end}
                    </small>
                    <pre>{span.preview || '（无文字）'}</pre>
                  </div>
                ))}
                {!!candidate.reasons?.length && (
                  <p className="field-help">{candidate.reasons.join(' · ')}</p>
                )}
                {!!candidate.alternatives.length && (
                  <details>
                    <summary>查看候选图书与匹配证据</summary>
                    {candidate.alternatives.map((alternative: RecordData) => (
                      <div key={`${alternative.book_id}:${alternative.edition_id}`}>
                        <strong>
                          {alternative.book_title} · {alternative.edition_label}
                        </strong>
                        {alternative.evidence.map((evidence: RecordData, index: number) => (
                          <div key={index} className="alignment-ranges">
                            <small>
                              来源 {evidence.span?.source_id?.slice(0, 8)} · 匹配{' '}
                              {evidence.match_chars} 字符
                            </small>
                            <pre>{evidence.preview}</pre>
                          </div>
                        ))}
                        <JsonDetails title="查看原始匹配区间" value={alternative.evidence} />
                      </div>
                    ))}
                  </details>
                )}
                <fieldset disabled={!!saved || stale || busy > 0} className="guided-fields">
                  <label>
                    本条决定
                    <select
                      value={choice.action || ''}
                      onChange={(event) =>
                        update(candidate.id, {
                          action: (event.target.value || undefined) as Choice['action'],
                        })
                      }
                    >
                      <option value="">暂缓</option>
                      <option value="confirm">确认归属</option>
                      <option value="reject">拒绝建议</option>
                    </select>
                  </label>
                  {choice.action === 'confirm' && (
                    <label>
                      归属目标
                      <select
                        value={choice.target || ''}
                        onChange={(event) => update(candidate.id, { target: event.target.value })}
                      >
                        <option value="" disabled={!candidate.suggested_target}>
                          {candidate.suggested_target ? '使用建议图书与版本' : '请选择目标'}
                        </option>
                        {candidate.alternatives.map((alternative: RecordData, index: number) => (
                          <option key={index} value={index}>
                            {alternative.book_title} · {alternative.edition_label}
                          </option>
                        ))}
                        <option value="new">新建图书与版本</option>
                        <option value="existing">选择其他已有图书与版本</option>
                      </select>
                    </label>
                  )}
                  {choice.action === 'confirm' && choice.target === 'existing' && (
                    <AssignmentTargetFields
                      value={choice.existing || { book_id: '', edition_id: '' }}
                      onChange={(existing) => update(candidate.id, { existing })}
                      version={state.event_id}
                    />
                  )}
                  {choice.action === 'confirm' && choice.target === 'new' && (
                    <div className="guided-fields">
                      <label>
                        新书名
                        <input
                          value={choice.title || ''}
                          placeholder={candidate.title_hint || '填写书名'}
                          onChange={(event) => update(candidate.id, { title: event.target.value })}
                        />
                      </label>
                      <label>
                        作者
                        <input
                          value={choice.author || ''}
                          onChange={(event) => update(candidate.id, { author: event.target.value })}
                        />
                      </label>
                      <label>
                        版本名称
                        <input
                          value={choice.edition || ''}
                          onChange={(event) =>
                            update(candidate.id, { edition: event.target.value })
                          }
                        />
                      </label>
                      <label className="checkbox-label">
                        <input
                          type="checkbox"
                          checked={!!choice.groupNew}
                          onChange={(event) =>
                            update(candidate.id, { groupNew: event.target.checked })
                          }
                        />
                        与本批同名、同作者、同版本且已勾选合并的区间共用新建图书
                      </label>
                    </div>
                  )}
                </fieldset>
              </article>
            );
          })}
          <Pager
            page={pagination.page}
            total={rows.length}
            size={pagination.size}
            onChange={pagination.setPage}
          />
          {!saved && (
            <button
              className="button primary"
              disabled={
                busy > 0 ||
                stale ||
                !decisions.length ||
                decisions.some(
                  ({ candidate, choice }) =>
                    choice.action === 'confirm' && !targetOf(candidate, choice),
                )
              }
              onClick={() => void apply()}
            >
              保存 {decisions.length} 项决定
            </button>
          )}
        </>
      )}
    </section>
  );
}
