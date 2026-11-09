import { useEffect, useState } from 'react';
import { post } from './api';
import { useCollection } from './collections';
import { Issues, JsonDetails, Pager, Search, shortId } from './components';
import { useWorkbench } from './store';
import type { RecordData, Revision } from './types';

const fields = {
  title: '题名',
  author: '作者',
  language: '语言',
  edition: '版本标签',
  translator: '译者',
};

export default function RevisionAdvanced({
  revision,
  branch,
  isHead,
  textDirty,
  onUpdated,
  onDirtyChange,
}: {
  revision: Revision;
  branch?: RecordData;
  isHead: boolean;
  textDirty: boolean;
  onUpdated: (result: RecordData) => void;
  onDirtyChange: (dirty: boolean) => void;
}) {
  const { run, busy, notify } = useWorkbench();
  const [opened, setOpened] = useState(false);
  const [query, setQuery] = useState('');
  const [name, setName] = useState('校订副本');
  const [other, setOther] = useState('');
  const [result, setResult] = useState<RecordData | null>(null);
  const [resolutions, setResolutions] = useState<RecordData>({});
  const [metadata, setMetadata] = useState<RecordData>({});
  const revisions = useCollection(
    'revisions',
    {
      book_id: revision.book_id,
      edition_id: revision.edition_id,
      search: query.trim(),
    },
    20,
    opened,
  );
  useEffect(() => {
    setMetadata(
      Object.fromEntries(Object.keys(fields).map((key) => [key, revision.metadata?.[key] || ''])),
    );
    setResult(null);
    setResolutions({});
    setOther('');
  }, [revision.id]);
  const changed = Object.fromEntries(
    Object.entries(metadata).filter(([key, value]) => value !== (revision.metadata?.[key] || '')),
  );
  const metadataDirty = Object.keys(changed).length > 0;
  useEffect(() => {
    onDirtyChange(metadataDirty);
    return () => onDirtyChange(false);
  }, [metadataDirty, onDirtyChange]);
  const disabled = busy > 0 || textDirty;
  const merge = async () => {
    if (!branch || !other) return;
    const data = await run('', () =>
      post(`/api/branches/${branch.id}/merge`, {
        expected_revision_id: revision.id,
        other_revision_id: other,
        resolutions,
      }),
    );
    if (!data) return;
    setResult(data);
    if (data.merged) {
      notify(
        data.unchanged ? '两个版本没有需要合入的文字差异。' : '已合并为新修订，来源和旧版本保留。',
        'success',
      );
      onUpdated(data);
    } else notify('合并尚未应用。请逐项处理冲突或查看未解决原因。');
  };
  return (
    <details
      className="revision-advanced"
      onToggle={(event) => setOpened(event.currentTarget.open)}
    >
      <summary>分支、合并与整理稿元数据</summary>
      <p className="muted">
        从同一稿件分出不同校订方案；合并只处理同书、同版本且有共同祖先的文字修改。
      </p>
      <form
        className="advanced-row"
        onSubmit={async (event) => {
          event.preventDefault();
          const data = await run('整理分支已创建。', () =>
            post(`/api/revisions/${revision.id}/fork`, { name }),
          );
          if (data) onUpdated({ revision_id: data.head_revision_id, branch_id: data.id });
        }}
      >
        <label>
          新分支名称
          <input
            required
            maxLength={200}
            value={name}
            onChange={(event) => setName(event.target.value)}
          />
        </label>
        <button className="button" disabled={disabled || metadataDirty || !name.trim()}>
          从当前稿创建分支
        </button>
      </form>
      <Search
        value={query}
        onChange={(value) => {
          setQuery(value);
          revisions.setPage(0);
        }}
        placeholder="按题名或编号搜索同版本修订"
      />
      <div className="advanced-row">
        <label>
          合入其他修订
          <select
            aria-label="合入其他修订"
            value={other}
            disabled={disabled || metadataDirty || !isHead}
            onChange={(event) => {
              setOther(event.target.value);
              setResult(null);
              setResolutions({});
            }}
          >
            <option value="">选择同版本修订</option>
            {revisions.items
              .filter((r) => r.id !== revision.id)
              .map((r) => (
                <option key={r.id} value={r.id}>
                  {r.metadata?.title || '整理稿'} · {shortId(r.id)}
                </option>
              ))}
            {other && !revisions.items.some((r) => r.id === other) && (
              <option value={other}>{shortId(other)}</option>
            )}
          </select>
        </label>
        <button
          className="button"
          disabled={disabled || metadataDirty || !isHead || !other}
          onClick={() => void merge()}
        >
          比较并合并
        </button>
      </div>
      <Pager
        page={revisions.page}
        total={revisions.total}
        size={revisions.size}
        onChange={revisions.setPage}
      />
      <Issues issues={revisions.error ? [revisions.error] : []} />
      {result && !result.merged && (
        <section className="merge-conflicts">
          <Issues
            issues={(result.unresolved || []).map((item: RecordData) => ({
              ...item,
              message:
                (
                  {
                    no_common_ancestor: '两个稿件没有共同祖先，请在来源对照中手工整理。',
                    metadata_difference: '元数据不同，请先确认并统一题名、版本等信息。',
                    unsupported_structure_change: '存在结构或资源变化，需要手工对照。',
                    text_budget: '文本超过本次合并范围，需要分段手工整理。',
                    diff_budget: '差异过多，无法可靠自动合并。',
                  } as RecordData
                )[item.code] || item.code,
            }))}
          />
          {(result.conflicts || []).map((conflict: RecordData) => {
            const id = conflict.id || conflict.conflict_id;
            return (
              <article className="merge-conflict" key={id}>
                <h3>
                  冲突 · 块 {shortId(conflict.block_id)} · {conflict.start}–{conflict.end}
                </h3>
                <details>
                  <summary>共同原文</summary>
                  <pre>{conflict.base}</pre>
                </details>
                <div className="diff-grid">
                  <div>
                    <span>当前稿</span>
                    <pre>{conflict.left}</pre>
                  </div>
                  <div>
                    <span>合入稿</span>
                    <pre>{conflict.right}</pre>
                  </div>
                </div>
                <label>
                  此处采用
                  <select
                    aria-label={`解决冲突 ${shortId(id)}`}
                    value={resolutions[id]?.choice || ''}
                    onChange={(event) =>
                      setResolutions((previous) => ({
                        ...previous,
                        [id]: {
                          choice: event.target.value,
                          ...(event.target.value === 'custom' ? { text: conflict.left } : {}),
                        },
                      }))
                    }
                  >
                    <option value="">请选择</option>
                    <option value="left">保留当前稿</option>
                    <option value="right">采用合入稿</option>
                    <option value="custom">手工填写</option>
                  </select>
                </label>
                {resolutions[id]?.choice === 'custom' && (
                  <textarea
                    aria-label={`自定义冲突 ${shortId(id)}`}
                    value={resolutions[id].text}
                    onChange={(event) =>
                      setResolutions((previous) => ({
                        ...previous,
                        [id]: { choice: 'custom', text: event.target.value },
                      }))
                    }
                  />
                )}
              </article>
            );
          })}
          {!!result.conflicts?.length && (
            <button
              className="button primary"
              disabled={
                disabled ||
                metadataDirty ||
                !isHead ||
                result.conflicts.some(
                  (c: RecordData) => !resolutions[c.id || c.conflict_id]?.choice,
                )
              }
              onClick={() => void merge()}
            >
              应用冲突选择并合并
            </button>
          )}
          <JsonDetails title="查看合并报告" value={result} />
        </section>
      )}
      <form
        className="stack-form"
        onSubmit={async (event) => {
          event.preventDefault();
          if (!branch) return;
          const data = await run('元数据已保存到新修订。', () =>
            post(`/api/branches/${branch.id}/metadata`, {
              expected_revision_id: revision.id,
              metadata: changed,
            }),
          );
          if (data) onUpdated(data);
        }}
      >
        <h3>整理稿元数据</h3>
        <div className="advanced-metadata">
          {Object.entries(fields).map(([key, label]) => (
            <label key={key}>
              {label}
              <input
                aria-label={`整理稿${label}`}
                maxLength={1000}
                required={key === 'title' || key === 'language'}
                value={metadata[key] || ''}
                readOnly={!isHead || textDirty}
                onChange={(event) =>
                  setMetadata((previous) => ({ ...previous, [key]: event.target.value }))
                }
              />
            </label>
          ))}
        </div>
        <div className="button-group">
          <button className="button primary" disabled={disabled || !isHead || !metadataDirty}>
            保存整理稿元数据
          </button>
          {metadataDirty && (
            <button
              className="text-button"
              type="button"
              onClick={() =>
                setMetadata(
                  Object.fromEntries(
                    Object.keys(fields).map((key) => [key, revision.metadata?.[key] || '']),
                  ),
                )
              }
            >
              放弃元数据修改
            </button>
          )}
        </div>
      </form>
    </details>
  );
}
