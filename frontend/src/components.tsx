import { useEffect, useState, type ReactNode } from 'react';
import type { RecordData } from './types';

const paths: Record<string, ReactNode> = {
  sources: (
    <>
      <path d="M4 5h6l2 3h8v11H4z" />
      <path d="M4 8h8" />
    </>
  ),
  books: (
    <>
      <path d="M4 4h6v16H4zM14 4h6v16h-6z" />
      <path d="M6 8h2m8 0h2M6 16h2m8 0h2" />
    </>
  ),
  workspace: (
    <>
      <rect x="3" y="4" width="18" height="16" rx="2" />
      <path d="M10 4v16m5-12h3m-3 4h3m-3 4h3" />
    </>
  ),
  review: (
    <>
      <path d="m8 12 3 3 5-6" />
      <path d="M7 3h10l4 4v13H3V3z" />
    </>
  ),
  jobs: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5l3 2" />
    </>
  ),
  exports: (
    <>
      <path d="M12 3v12m-4-4 4 4 4-4M4 16v5h16v-5" />
    </>
  ),
  settings: (
    <>
      <path d="M4 7h16M4 17h16" />
      <circle cx="9" cy="7" r="3" />
      <circle cx="16" cy="17" r="3" />
    </>
  ),
  plus: <path d="M12 5v14M5 12h14" />,
  upload: (
    <>
      <path d="M12 16V4m-4 4 4-4 4 4M4 16v5h16v-5" />
    </>
  ),
  search: (
    <>
      <circle cx="10" cy="10" r="6" />
      <path d="m15 15 5 5" />
    </>
  ),
  arrow: <path d="M4 12h16m-6-6 6 6-6 6" />,
  check: <path d="m5 12 4 4L19 6" />,
  refresh: (
    <>
      <path d="M20 7v5h-5M4 17v-5h5" />
      <path d="M6 7a7 7 0 0 1 12-2l2 2M4 17l2 2a7 7 0 0 0 12-2" />
    </>
  ),
};
export function Icon({ name, size = 19 }: { name: string; size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {paths[name] || paths.books}
    </svg>
  );
}
const statusLabels: Record<string, string> = {
  needs_review: '需审阅',
  needs_attention: '需关注',
  queued: '排队中',
  running: '处理中',
  paused: '已暂停',
  waiting_review: '等待审阅',
  succeeded: '已完成',
  completed: '已完成',
  ready: '已就绪',
  failed: '失败',
  cancelled: '已取消',
  imported: '已导入',
  extracted: '已提取',
  pending: '待审阅',
  accepted: '已接受',
  rejected: '已拒绝',
  draft: '草稿',
  finalized: '已定版',
  final: '已定版',
  unsupported: '未支持',
  error: '需处理',
  conflict: '有冲突',
  stale: '已过期',
};
export function Badge({ status = '', children }: { status?: string; children?: ReactNode }) {
  return (
    <span className={`badge status-${status}`}>
      {children || statusLabels[status] || status || '未标记'}
    </span>
  );
}
export function Empty({
  title,
  children,
  icon = 'books',
}: {
  title: string;
  children?: ReactNode;
  icon?: string;
}) {
  return (
    <div className="empty">
      <div className="empty-icon">
        <Icon name={icon} size={28} />
      </div>
      <h3>{title}</h3>
      {children && <div className="muted">{children}</div>}
    </div>
  );
}
export function SectionHeading({
  eyebrow,
  title,
  description,
  actions,
}: {
  eyebrow?: string;
  title: string;
  description?: string;
  actions?: ReactNode;
}) {
  return (
    <header className="section-heading">
      <div>
        {eyebrow && <div className="eyebrow">{eyebrow}</div>}
        <h1>{title}</h1>
        {description && <p>{description}</p>}
      </div>
      {actions && <div className="heading-actions">{actions}</div>}
    </header>
  );
}
export function Search({
  value,
  onChange,
  placeholder = '搜索',
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
}) {
  return (
    <label className="search-field">
      <Icon name="search" size={17} />
      <input
        aria-label={placeholder}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
      />
    </label>
  );
}
export function Pager({
  page,
  total,
  size,
  onChange,
}: {
  page: number;
  total: number;
  size: number;
  onChange: (page: number) => void;
}) {
  const count = Math.max(1, Math.ceil(total / size));
  return (
    <div className="pager">
      <span>
        共 {total.toLocaleString()} 项 · 第 {Math.min(page + 1, count)} / {count} 页
      </span>
      <div>
        <button className="button small" disabled={page === 0} onClick={() => onChange(page - 1)}>
          上一页
        </button>
        <button
          className="button small"
          disabled={page + 1 >= count}
          onClick={() => onChange(page + 1)}
        >
          下一页
        </button>
      </div>
    </div>
  );
}
export function usePagination<T>(items: T[], size = 20) {
  const [page, setPage] = useState(0);
  useEffect(() => {
    if (page * size >= items.length) setPage(0);
  }, [items.length, page, size]);
  return { page, setPage, size, visible: items.slice(page * size, (page + 1) * size) };
}
export function Issues({ issues }: { issues?: any[] | RecordData }) {
  const list = Array.isArray(issues) ? issues : issues ? [issues] : [];
  if (!list.length) return null;
  return (
    <div className="issues">
      {list.map((issue, index) => (
        <div key={index}>
          <span className="issue-dot">!</span>
          <span>
            {typeof issue === 'string'
              ? issue
              : issue.message || issue.description || issue.code || JSON.stringify(issue)}
          </span>
        </div>
      ))}
    </div>
  );
}
export function bytes(value: unknown) {
  const count = Number(value || 0);
  if (count < 1024) return `${count} B`;
  if (count < 1024 ** 2) return `${(count / 1024).toFixed(1)} KB`;
  if (count < 1024 ** 3) return `${(count / 1024 ** 2).toFixed(1)} MB`;
  return `${(count / 1024 ** 3).toFixed(2)} GB`;
}
export function date(value: unknown) {
  if (!value) return '—';
  const parsed = new Date(
    typeof value === 'number' ? (value < 1e12 ? value * 1000 : value) : String(value),
  );
  return Number.isNaN(parsed.getTime())
    ? String(value)
    : parsed.toLocaleString('zh-CN', {
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
      });
}
export function shortId(value: unknown) {
  return value ? String(value).slice(0, 8) : '—';
}
export function JsonDetails({ title = '查看详情', value }: { title?: string; value: unknown }) {
  return (
    <details className="json-details">
      <summary>{title}</summary>
      <pre>{JSON.stringify(value, null, 2)}</pre>
    </details>
  );
}
