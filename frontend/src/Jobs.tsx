import { useState } from 'react';
import { post } from './api';
import { useCollection } from './collections';
import {
  Badge,
  Empty,
  Issues,
  JsonDetails,
  Pager,
  Search,
  SectionHeading,
  date,
  shortId,
} from './components';
import { useWorkbench } from './store';
import { canCancelJob, jobRecoveryRequest } from './types';

const kinds: Record<string, string> = {
  import: '来源导入',
  extract: '提取文档',
  analyze: '分析来源',
  analyze_source: '分析来源',
  analyze_book: '分析版本',
  review: 'AI 校订',
  ai_review: 'AI 校订',
  export: '导出整理稿',
  backup: '项目备份',
};
const filterStates: Record<string, string> = {
  active: 'queued,running,paused',
  failed: 'failed,needs_attention',
  done: 'succeeded',
};

export default function Jobs() {
  const { state, run, busy } = useWorkbench();
  const [filter, setFilter] = useState('all');
  const [query, setQuery] = useState('');
  const counts = state.state_counts?.jobs || {};
  const pagination = useCollection(
    'jobs',
    { state: filterStates[filter], search: query.trim() },
    15,
  );
  return (
    <>
      <SectionHeading
        eyebrow="TASK CENTER"
        title="处理进展，一目了然"
        description="任务保留状态与结果。关闭浏览器后仍继续处理，可在这里暂停或恢复。"
      />
      <div className="stats-row">
        <div>
          <span>进行中</span>
          <strong>{(counts.running ?? 0) + (counts.queued ?? 0)}</strong>
        </div>
        <div>
          <span>等待处理</span>
          <strong>{counts.paused ?? 0}</strong>
        </div>
        <div>
          <span>已完成</span>
          <strong>{counts.succeeded ?? 0}</strong>
        </div>
        <div>
          <span>需关注</span>
          <strong>{(counts.failed ?? 0) + (counts.needs_attention ?? 0)}</strong>
        </div>
      </div>
      <div className="list-toolbar">
        <Search
          value={query}
          onChange={(value) => {
            setQuery(value);
            pagination.setPage(0);
          }}
          placeholder="搜索任务编号"
        />
        <div className="filter-tabs">
          {[
            ['all', '全部任务'],
            ['active', '进行与等待'],
            ['failed', '需关注'],
            ['done', '已完成'],
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
            </button>
          ))}
        </div>
      </div>
      <Issues issues={pagination.error ? [pagination.error] : []} />
      <div className="job-list">
        {pagination.items.map((job) => {
          const progress =
            typeof job.progress === 'number'
              ? job.progress <= 1
                ? job.progress * 100
                : job.progress
              : job.progress?.percent;
          return (
            <article className="card job-card" key={job.id}>
              <div className="job-main">
                <div className="job-symbol">
                  {job.status === 'running' ? (
                    <span className="spinner" />
                  ) : job.status === 'succeeded' ? (
                    '✓'
                  ) : ['failed', 'needs_attention'].includes(job.status) ? (
                    '!'
                  ) : (
                    '·'
                  )}
                </div>
                <div className="job-title">
                  <h3>
                    {job.title || kinds[job.kind || job.type] || job.kind || job.type || '处理任务'}
                  </h3>
                  <p>
                    {date(job.created_at)} · {shortId(job.id)}
                    {job.attempt ? ` · 第 ${job.attempt} 次尝试` : ''}
                  </p>
                </div>
                <Badge status={job.status} />
                <div className="button-group">
                  {['queued', 'running'].includes(job.status) && (
                    <button
                      className="button small"
                      disabled={busy > 0}
                      onClick={() =>
                        void run('已请求暂停任务。', () => post(`/api/jobs/${job.id}/pause`))
                      }
                    >
                      暂停
                    </button>
                  )}
                  {['paused', 'failed', 'needs_attention'].includes(job.status) && (
                    <button
                      className="button small"
                      disabled={busy > 0}
                      onClick={() => {
                        const request = jobRecoveryRequest(job.id, job.status, (message) =>
                          window.confirm(message),
                        );
                        if (request)
                          void run(
                            job.status === 'paused' ? '任务已请求恢复。' : '任务重试已提交。',
                            () => post(request.path, request.body),
                          );
                      }}
                    >
                      {job.status === 'needs_attention'
                        ? '核实后重新请求'
                        : job.status === 'failed'
                          ? '重试'
                          : '恢复'}
                    </button>
                  )}
                  {canCancelJob(job.status) && (
                    <button
                      className="button small"
                      disabled={busy > 0}
                      onClick={() =>
                        void run('已请求取消任务。', () => post(`/api/jobs/${job.id}/cancel`))
                      }
                    >
                      取消任务
                    </button>
                  )}
                </div>
              </div>
              {progress !== undefined && (
                <div className="progress-line">
                  <span style={{ width: `${Math.max(0, Math.min(100, progress))}%` }} />
                </div>
              )}
              {job.message && <p className="muted">{job.message}</p>}
              <Issues
                issues={
                  job.error
                    ? [typeof job.error === 'object' ? job.error : { message: job.error }]
                    : []
                }
              />
              {job.result && <JsonDetails title="查看处理结果与建议" value={job.result} />}
            </article>
          );
        })}
      </div>
      {!pagination.loading && !pagination.error && !pagination.items.length && (
        <Empty
          title={filter !== 'all' || query.trim() ? '没有符合条件的任务' : '这里会记录每一次处理'}
          icon="jobs"
        >
          <p>导入、来源分析和AI校订的进展与结果，都集中在这里。</p>
        </Empty>
      )}
      <Pager
        page={pagination.page}
        total={pagination.total}
        size={pagination.size}
        onChange={pagination.setPage}
      />
    </>
  );
}
