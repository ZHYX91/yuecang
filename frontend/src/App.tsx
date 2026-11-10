import { useEffect, useRef, useState } from 'react';
import { api } from './api';
import { Icon } from './components';
import { useWorkbench } from './store';
import { spanKey, type Book, type Page, type Revision, type SelectedSpan } from './types';
import Sources from './Sources';
import Books from './Books';
import Workspace from './Workspace';
import Review from './Review';
import Jobs from './Jobs';
import Exports from './Exports';
import Settings from './Settings';

const navigation: { id: Page; name: string; hint: string }[] = [
  { id: 'sources', name: '来源与导入', hint: '收集与查看原件' },
  { id: 'books', name: '图书与版本', hint: '确认图书身份' },
  { id: 'workspace', name: '整理工作台', hint: '对照、选择与编辑' },
  { id: 'review', name: '校订待办', hint: '审阅修改建议' },
  { id: 'jobs', name: '任务中心', hint: '进度与处理结果' },
  { id: 'exports', name: '整理稿导出', hint: '带走整理成果' },
  { id: 'settings', name: '项目设置', hint: '模型、存储与备份' },
];
export default function App() {
  const { state, ready, loading, connected, busy, refresh, notify, hasUnsavedEdits, remember } =
    useWorkbench();
  const [page, setPage] = useState<Page>('sources');
  const [visited, setVisited] = useState<Set<Page>>(new Set(['sources']));
  const [sourceId, setSourceId] = useState('');
  const [bookId, setBookId] = useState('');
  const [browseBookId, setBrowseBookId] = useState('');
  const [revisionId, setRevisionId] = useState('');
  const [requestId, setRequestId] = useState('');
  const [selections, setSelections] = useState<SelectedSpan[]>([]);
  const openingRevision = useRef<AbortController | null>(null);
  useEffect(() => () => openingRevision.current?.abort(), []);
  const navigate = (next: Page) => {
    openingRevision.current?.abort();
    setPage(next);
    setVisited((previous) => new Set(previous).add(next));
    window.scrollTo({ top: 0 });
  };
  const add = (spans: SelectedSpan[]) => {
    setSelections((previous) => {
      const keys = new Set(previous.map(spanKey));
      return [
        ...previous,
        ...spans.filter((span) => {
          const key = spanKey(span);
          if (keys.has(key)) return false;
          keys.add(key);
          return true;
        }),
      ];
    });
  };
  const remove = (key: string) =>
    setSelections((previous) => previous.filter((span) => spanKey(span) !== key));
  const reorder = (index: number, delta: number) =>
    setSelections((previous) => {
      const next = [...previous];
      const target = index + delta;
      if (target < 0 || target >= next.length) return next;
      [next[index], next[target]] = [next[target], next[index]];
      return next;
    });
  const openRevision = async (id: string) => {
    if (hasUnsavedEdits && id !== revisionId) {
      notify('当前整理稿尚有未保存修改，请先保存或放弃修改。');
      navigate('workspace');
      return;
    }
    openingRevision.current?.abort();
    const controller = new AbortController();
    openingRevision.current = controller;
    const init = { signal: controller.signal };
    try {
      const revision =
        state.revisions.find((item) => item.id === id) ??
        (await api<Revision>(`/api/revisions/${encodeURIComponent(id)}?summary=true`, init));
      const book =
        state.books.find((item) => item.id === revision.book_id) ??
        (await api<Book>(`/api/books/${encodeURIComponent(revision.book_id)}`, init));
      if (controller.signal.aborted) return;
      remember('revisions', [revision]);
      remember('books', [book]);
      setBookId(book.id);
      setRevisionId(revision.id);
      navigate('workspace');
    } catch (error) {
      if (!controller.signal.aborted)
        notify(error instanceof Error ? error.message : '无法打开整理稿。', 'error');
    }
  };
  const pending = state.state_counts?.proposals?.pending ?? 0;
  const running =
    (state.state_counts?.jobs?.queued ?? 0) + (state.state_counts?.jobs?.running ?? 0);
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <a
          className="brand"
          href="#"
          onClick={(event) => {
            event.preventDefault();
            navigate('sources');
          }}
        >
          <span className="brand-mark">阅</span>
          <span>
            <strong>阅藏</strong>
            <small>YUECANG</small>
          </span>
        </a>
        <div className="sidebar-caption">图书整理工作台</div>
        <nav aria-label="工作台导航">
          {navigation.map((item) => (
            <button
              key={item.id}
              className={`nav-item ${page === item.id ? 'active' : ''}`}
              aria-label={item.name}
              aria-current={page === item.id ? 'page' : undefined}
              title={item.hint}
              onClick={() => navigate(item.id)}
            >
              <Icon name={item.id} />
              <span>{item.name}</span>
              {item.id === 'review' && pending > 0 && <b>{pending}</b>}
              {item.id === 'jobs' && running > 0 && <b>{running}</b>}
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="sidebar-project">
            <span className="project-dot" />
            <div>
              <strong>{state.project?.name || '本地资料库'}</strong>
              <small>
                {state.totals?.sources ?? state.sources.length} 份来源 ·{' '}
                {state.totals?.books ?? state.books.length} 本图书
              </small>
            </div>
          </div>
          <p>保留来源，细致整理。</p>
          <span className="version-label">LOCAL WORKBENCH · 0.3.0</span>
        </div>
      </aside>
      <div className="main-shell">
        <header className="topbar">
          <div className="breadcrumb">
            我的工作区 <span>/</span>{' '}
            <strong>{navigation.find((item) => item.id === page)?.name}</strong>
          </div>
          <div className="topbar-actions">
            {busy > 0 && (
              <span className="working-label">
                <span className="spinner" />
                正在处理
              </span>
            )}
            <span className={`connection-indicator ${connected ? 'online' : ''}`}>
              <i />
              {connected ? '本地服务已连接' : loading ? '连接中' : '连接中断'}
            </span>
            <button
              className="icon-button"
              aria-label="刷新工作区"
              title="刷新工作区"
              onClick={() => void refresh()}
            >
              <Icon name="refresh" size={17} />
            </button>
          </div>
        </header>
        <main className="main-content">
          {!ready && loading ? (
            <div className="startup-state">
              <span className="spinner" />
              <h2>正在打开阅藏工作台</h2>
              <p>读取本地项目与任务状态…</p>
            </div>
          ) : !ready ? (
            <div className="startup-state">
              <Icon name="sources" size={34} />
              <h2>暂时无法连接本地服务</h2>
              <p>请保持阅藏启动器运行，再重新连接。尚未读取任何项目内容。</p>
              <button className="button primary" onClick={() => void refresh()}>
                重新连接
              </button>
            </div>
          ) : (
            <>
              {visited.has('sources') && (
                <div hidden={page !== 'sources'}>
                  <Sources
                    selected={sourceId}
                    setSelected={setSourceId}
                    selections={selections}
                    add={add}
                    remove={remove}
                    openWorkspace={() => navigate('workspace')}
                  />
                </div>
              )}
              {visited.has('books') && (
                <div hidden={page !== 'books'}>
                  <Books
                    selected={browseBookId || bookId}
                    setSelected={setBrowseBookId}
                    openWorkspace={(id, revision) => {
                      if (
                        hasUnsavedEdits &&
                        (id !== bookId || (revision && revision !== revisionId))
                      ) {
                        notify('当前整理稿尚有未保存修改，请先保存或放弃修改。');
                        navigate('workspace');
                        return;
                      }
                      if (revision) {
                        void openRevision(revision);
                        return;
                      }
                      setBookId(id);
                      if (id !== bookId) setRevisionId('');
                      navigate('workspace');
                    }}
                  />
                </div>
              )}
              {visited.has('workspace') && (
                <div hidden={page !== 'workspace'}>
                  <Workspace
                    bookId={bookId}
                    setBookId={setBookId}
                    sourceId={sourceId}
                    revisionId={revisionId}
                    setRevisionId={setRevisionId}
                    selections={selections}
                    add={add}
                    remove={remove}
                    reorder={reorder}
                    clear={() => setSelections([])}
                    openBooks={() => navigate('books')}
                    onReviewRequest={(id) => {
                      setRequestId(id);
                      navigate('review');
                    }}
                  />
                </div>
              )}
              {visited.has('review') && (
                <div hidden={page !== 'review'}>
                  <Review
                    requestId={requestId}
                    setRequestId={setRequestId}
                    openRevision={openRevision}
                  />
                </div>
              )}
              {visited.has('jobs') && (
                <div hidden={page !== 'jobs'}>
                  <Jobs />
                </div>
              )}
              {visited.has('exports') && (
                <div hidden={page !== 'exports'}>
                  <Exports preferredRevision={revisionId} openRevision={openRevision} />
                </div>
              )}
              {visited.has('settings') && (
                <div hidden={page !== 'settings'}>
                  <Settings />
                </div>
              )}
            </>
          )}
        </main>
        <footer className="page-footer">
          <span>阅藏 · 本地图书整理工作台</span>
          <button
            className="text-button"
            onClick={() => {
              notify('正文与编辑记录保存在当前本地项目。模型审阅仅在主动发起后连接已配置服务。');
            }}
          >
            数据保存在何处
          </button>
        </footer>
      </div>
    </div>
  );
}
