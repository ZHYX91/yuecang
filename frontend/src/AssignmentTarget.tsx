import { useEffect, useState } from 'react';
import { api } from './api';
import { Issues } from './components';
import type { RecordData } from './types';

export type AssignmentTarget = { book_id: string; edition_id: string };

export function useTargetRows(collection: string, query: string, enabled = true, version = 0) {
  const [result, setResult] = useState<{ key: string; items: RecordData[]; error?: string }>();
  const key = JSON.stringify([collection, query, version]);
  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void api<{ items: RecordData[] }>(`/api/${collection}?${query}`, {
        signal: controller.signal,
      })
        .then(({ items }) => {
          if (!controller.signal.aborted) setResult({ key, items });
        })
        .catch((error) => {
          if (!controller.signal.aborted) setResult({ key, items: [], error: error.message });
        });
    }, 180);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [collection, enabled, key, query]);
  return result?.key === key ? result : { items: [] as RecordData[], error: undefined };
}

export function AssignmentTargetFields({
  value,
  onChange,
  version = 0,
}: {
  value: AssignmentTarget;
  onChange: (value: AssignmentTarget) => void;
  version?: number;
}) {
  const [query, setQuery] = useState('');
  const [editionQuery, setEditionQuery] = useState('');
  const [pickedBook, setPickedBook] = useState<RecordData>();
  const [pickedEdition, setPickedEdition] = useState<RecordData>();
  const books = useTargetRows(
    'books',
    `limit=25&search=${encodeURIComponent(query.trim())}`,
    true,
    version,
  );
  const editions = useTargetRows(
    'editions',
    `limit=25&book_id=${encodeURIComponent(value.book_id)}&search=${encodeURIComponent(editionQuery.trim())}`,
    !!value.book_id,
    version,
  );
  const book = pickedBook?.id === value.book_id ? pickedBook : undefined;
  const edition = pickedEdition?.id === value.edition_id ? pickedEdition : undefined;
  useEffect(() => {
    const controller = new AbortController();
    const load = (collection: string, id: string, save: (row: RecordData) => void) =>
      api(`/api/${collection}/${id}`, { signal: controller.signal })
        .then((row) => {
          if (!controller.signal.aborted) save(row);
        })
        .catch(() => {
          /* The picker can still search for a replacement. */
        });
    if (value.book_id && !book) void load('books', value.book_id, setPickedBook);
    if (value.edition_id && !edition) void load('editions', value.edition_id, setPickedEdition);
    return () => controller.abort();
  }, [book, edition, value.book_id, value.edition_id]);
  return (
    <div className="guided-fields">
      <label>
        搜索图书
        <input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="书名或作者"
        />
      </label>
      <label>
        目标图书
        <select
          aria-label="目标图书"
          value={value.book_id}
          onChange={(event) => {
            setPickedBook(books.items.find((item) => item.id === event.target.value));
            setEditionQuery('');
            onChange({ book_id: event.target.value, edition_id: '' });
          }}
        >
          <option value="">选择图书</option>
          {book && !books.items.some((item) => item.id === book.id) && (
            <option value={book.id}>{book.title}</option>
          )}
          {books.items.map((item) => (
            <option key={item.id} value={item.id}>
              {item.title}
              {item.author ? ` · ${item.author}` : ''}
            </option>
          ))}
        </select>
      </label>
      {value.book_id && (
        <label>
          搜索版本
          <input
            value={editionQuery}
            onChange={(event) => setEditionQuery(event.target.value)}
            placeholder="版本名称或译者"
          />
        </label>
      )}
      <label>
        目标版本
        <select
          aria-label="目标版本"
          disabled={!value.book_id}
          value={value.edition_id}
          onChange={(event) => {
            setPickedEdition(editions.items.find((item) => item.id === event.target.value));
            onChange({ ...value, edition_id: event.target.value });
          }}
        >
          <option value="">选择版本</option>
          {edition && !editions.items.some((item) => item.id === edition.id) && (
            <option value={edition.id}>{edition.label}</option>
          )}
          {value.book_id &&
            editions.items.map((item) => (
              <option key={item.id} value={item.id}>
                {item.label}
              </option>
            ))}
        </select>
      </label>
      <Issues issues={[books.error, editions.error].filter(Boolean)} />
    </div>
  );
}
