import { useEffect, useState } from 'react';
import { api, post } from './api';
import { useCollection } from './collections';
import {
  Badge,
  Empty,
  Icon,
  JsonDetails,
  Pager,
  Search,
  SectionHeading,
  shortId,
} from './components';
import { useWorkbench } from './store';
import type { Book } from './types';

export default function Books({
  selected,
  setSelected,
  openWorkspace,
}: {
  selected: string;
  setSelected: (id: string) => void;
  openWorkspace: (bookId: string, revisionId?: string) => void;
}) {
  const { state, run, busy, remember } = useWorkbench();
  const [query, setQuery] = useState('');
  const [creating, setCreating] = useState(false);
  const [title, setTitle] = useState('');
  const [author, setAuthor] = useState('');
  const [editionLabel, setEditionLabel] = useState('');
  const [language, setLanguage] = useState('zh');
  const [translator, setTranslator] = useState('');
  const [analysis, setAnalysis] = useState<{ bookId: string; result: unknown } | null>(null);
  const [bookError, setBookError] = useState<{ bookId: string; message: string } | null>(null);
  const pagination = useCollection('books', { search: query.trim() }, 18);
  const editionPage = useCollection('editions', { book_id: selected }, 20, !!selected);
  const branchPage = useCollection('branches', { book_id: selected }, 20, !!selected);
  const book = state.books.find((item) => item.id === selected);
  const missingBook = selected && !book ? selected : '';
  useEffect(() => {
    if (!missingBook) return;
    const controller = new AbortController();
    setBookError(null);
    void api<Book>(`/api/books/${encodeURIComponent(missingBook)}`, { signal: controller.signal })
      .then((value) => {
        if (!controller.signal.aborted) remember('books', [value]);
      })
      .catch((error) => {
        if (!controller.signal.aborted)
          setBookError({
            bookId: missingBook,
            message: error instanceof Error ? error.message : '无法读取图书。',
          });
      });
    return () => controller.abort();
  }, [missingBook, remember]);
  const createBook = async (event: React.FormEvent) => {
    event.preventDefault();
    const result = await run('图书已建立，可以开始归属来源。', () =>
      post('/api/books', { title: title.trim(), author: author.trim(), language: 'zh' }),
    );
    if (result) {
      if (result.book) remember('books', [result.book]);
      setSelected(result.book?.id || result.book_id || result.id);
      setCreating(false);
      setTitle('');
      setAuthor('');
    }
  };
  return (
    <>
      <SectionHeading
        eyebrow="BOOKS & EDITIONS"
        title="让来源找到归属"
        description="书籍身份保持稳定。不同译本、修订版和删节版，分别保留。"
        actions={
          <button className="button primary" onClick={() => setCreating(!creating)}>
            <Icon name="plus" />
            建立图书
          </button>
        }
      />
      {creating && (
        <form className="card inline-form" onSubmit={(event) => void createBook(event)}>
          <label>
            书名
            <input
              autoFocus
              required
              value={title}
              onChange={(event) => setTitle(event.target.value)}
              placeholder="输入准确书名"
            />
          </label>
          <label>
            作者
            <input
              value={author}
              onChange={(event) => setAuthor(event.target.value)}
              placeholder="可留空，后续确认"
            />
          </label>
          <button className="button primary" disabled={busy > 0 || !title.trim()}>
            建立
          </button>
          <button type="button" className="button" onClick={() => setCreating(false)}>
            取消
          </button>
        </form>
      )}
      {pagination.error && <p role="alert">{pagination.error}</p>}
      <div className="book-layout">
        <section>
          <div className="list-toolbar">
            <Search
              value={query}
              onChange={(value) => {
                setQuery(value);
                pagination.setPage(0);
              }}
              placeholder="搜索书名或作者"
            />
            <span className="muted">{pagination.total} 本图书</span>
          </div>
          <div className="book-grid">
            {pagination.items.map((item, index) => (
              <button
                key={item.id}
                className={`book-card ${selected === item.id ? 'selected' : ''}`}
                onClick={() => {
                  remember('books', [item]);
                  setSelected(item.id);
                }}
              >
                <div className={`book-cover cover-${index % 4}`}>
                  <span>阅藏</span>
                  <strong>{item.title.slice(0, 14)}</strong>
                  <i>YUECANG COLLECTION</i>
                </div>
                <div className="book-card-info">
                  <h3>{item.title}</h3>
                  <p>{item.author || '作者待确认'}</p>
                  <div>
                    <span>查看版本与整理稿</span>
                    <Badge status={item.default_revision_id ? 'finalized' : 'draft'} />
                  </div>
                </div>
              </button>
            ))}
          </div>
          {!pagination.loading && !pagination.error && !pagination.items.length && (
            <Empty title={query ? '没有找到图书' : '建立第一本图书'}>
              <p>将属于同一本书的片段归在一起，再选择底本生成整理稿。</p>
            </Empty>
          )}
          <Pager
            page={pagination.page}
            total={pagination.total}
            size={pagination.size}
            onChange={pagination.setPage}
          />
        </section>
        <aside className="card book-detail">
          {book ? (
            <>
              <div className="eyebrow">当前图书</div>
              <h2>{book.title}</h2>
              <p className="muted">{book.author || '作者待确认'}</p>
              <button className="button primary full-width" onClick={() => openWorkspace(book.id)}>
                进入整理台 <Icon name="arrow" />
              </button>
              <div className="detail-section">
                <div className="card-heading">
                  <h3>原有版本</h3>
                  <span className="count">{editionPage.total}</span>
                </div>
                {editionPage.items.length
                  ? editionPage.items.map((edition) => (
                      <div className="edition-row" key={edition.id}>
                        <strong>{edition.label || edition.name || '版本待确认'}</strong>
                        <span>
                          {edition.language || '语言待确认'}
                          {edition.translator ? ` · ${edition.translator} 译` : ''}
                        </span>
                      </div>
                    ))
                  : !editionPage.loading &&
                    !editionPage.error && <p className="muted">暂未区分版本。</p>}
                {editionPage.error && <p role="alert">{editionPage.error}</p>}
                <Pager
                  page={editionPage.page}
                  total={editionPage.total}
                  size={editionPage.size}
                  onChange={editionPage.setPage}
                />
                <form
                  className="stack-form small-form"
                  onSubmit={async (event) => {
                    event.preventDefault();
                    const result = await run('版本已添加。', () =>
                      post(`/api/books/${book.id}/editions`, {
                        label: editionLabel.trim(),
                        language,
                        translator: translator.trim(),
                      }),
                    );
                    if (result) {
                      setEditionLabel('');
                      setTranslator('');
                    }
                  }}
                >
                  <label>
                    添加版本
                    <input
                      required
                      placeholder="如：第二版 / 初版"
                      value={editionLabel}
                      onChange={(event) => setEditionLabel(event.target.value)}
                    />
                  </label>
                  <div className="form-row">
                    <label>
                      语言
                      <input
                        value={language}
                        onChange={(event) => setLanguage(event.target.value)}
                        placeholder="zh"
                      />
                    </label>
                    <label>
                      译者
                      <input
                        value={translator}
                        onChange={(event) => setTranslator(event.target.value)}
                        placeholder="可选"
                      />
                    </label>
                  </div>
                  <button className="button small" disabled={!editionLabel.trim() || busy > 0}>
                    添加版本
                  </button>
                </form>
              </div>
              <div className="detail-section">
                <h3>整理分支</h3>
                {branchPage.items.map((branch) => (
                  <button
                    className="branch-row"
                    key={branch.id}
                    onClick={() => openWorkspace(book.id, branch.head_revision_id)}
                  >
                    <span>{branch.name || '整理稿'}</span>
                    <code>{shortId(branch.head_revision_id)}</code>
                    <Icon name="arrow" size={15} />
                  </button>
                ))}
                {!branchPage.items.length && !branchPage.loading && !branchPage.error && (
                  <p className="muted">选择来源片段后创建整理稿。</p>
                )}
                {branchPage.error && <p role="alert">{branchPage.error}</p>}
                <Pager
                  page={branchPage.page}
                  total={branchPage.total}
                  size={branchPage.size}
                  onChange={branchPage.setPage}
                />
              </div>
              <button
                className="button full-width"
                disabled={busy > 0}
                onClick={async () => {
                  const result = await run('分析请求已处理，结果见下方。', () =>
                    post(`/api/books/${book.id}/analyze`),
                  );
                  if (result) setAnalysis({ bookId: book.id, result });
                }}
              >
                分析来源与版本
              </button>
              {analysis?.bookId === book.id && (
                <JsonDetails title="查看分析结果与待处理关系" value={analysis.result} />
              )}
            </>
          ) : bookError?.bookId === selected ? (
            <p role="alert">{bookError.message}</p>
          ) : selected ? (
            <p className="muted">正在读取图书…</p>
          ) : (
            <Empty title="选择一本图书">
              <p>查看版本、整理分支和来源关系。</p>
            </Empty>
          )}
        </aside>
      </div>
    </>
  );
}
