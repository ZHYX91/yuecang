import { useEffect, useRef, useState } from 'react';
import { post } from './api';
import { JsonDetails, SectionHeading, bytes } from './components';
import { useWorkbench } from './store';
import type { RecordData } from './types';

export default function Settings() {
  const { state, ready, run, busy } = useWorkbench();
  const initialized = useRef(false);
  const [endpoint, setEndpoint] = useState('');
  const [model, setModel] = useState('');
  const [key, setKey] = useState('');
  const [budget, setBudget] = useState('20000');
  const [maxTokens, setMaxTokens] = useState('3000');
  const [result, setResult] = useState<RecordData | null>(null);
  useEffect(() => {
    if (ready && !initialized.current) {
      const ai = state.settings.ai || {};
      setEndpoint(ai.endpoint || '');
      setModel(ai.model || '');
      setBudget(String(ai.token_budget ?? 20000));
      setMaxTokens(String(ai.max_output_tokens ?? 3000));
      initialized.current = true;
    }
  }, [ready, state.settings]);
  const storage = state.storage;
  return (
    <>
      <SectionHeading
        eyebrow="PROJECT SETTINGS"
        title="按你的方式工作"
        description="配置模型连接，查看容量，保存可恢复的项目副本。"
      />
      <div className="settings-layout">
        <section className="card settings-card">
          <div className="card-heading">
            <h2>模型服务</h2>
            <span className="local-chip">按需调用</span>
          </div>
          <p className="muted">
            只在你发起模型审阅时发送当前任务所需的文本与证据。没有模型连接，也可手动整理和离线审阅。
          </p>
          <form
            className="stack-form"
            onSubmit={async (event) => {
              event.preventDefault();
              const ai = {
                endpoint: endpoint.trim(),
                model: model.trim(),
                token_budget: Number(budget),
                max_output_tokens: Number(maxTokens),
                ...(key ? { api_key: key } : {}),
              };
              const saved = await run('模型设置已保存。', () => post('/api/settings', { ai }));
              if (saved) setKey('');
            }}
          >
            <label>
              服务地址
              <input
                type="url"
                value={endpoint}
                onChange={(event) => setEndpoint(event.target.value)}
                placeholder="https://服务地址/v1/chat/completions"
                autoComplete="off"
              />
            </label>
            <label>
              模型名称
              <input
                value={model}
                onChange={(event) => setModel(event.target.value)}
                placeholder="提供方的模型标识"
                autoComplete="off"
              />
            </label>
            <label>
              API 密钥
              <input
                type="password"
                value={key}
                onChange={(event) => setKey(event.target.value)}
                placeholder={
                  state.settings.ai?.has_api_key ||
                  state.settings.ai?.api_key_configured ||
                  state.settings.ai?.configured
                    ? '已有密钥，留空保持不变'
                    : '输入密钥；仅保存在本机设置中'
                }
                autoComplete="new-password"
              />
            </label>
            <div className="form-row">
              <label>
                单次请求 token 预算
                <input
                  type="number"
                  min="1"
                  step="1"
                  required
                  value={budget}
                  onChange={(event) => setBudget(event.target.value)}
                />
              </label>
              <label>
                单次最大输出 token
                <input
                  type="number"
                  min="1"
                  step="1"
                  required
                  value={maxTokens}
                  onChange={(event) => setMaxTokens(event.target.value)}
                />
              </label>
            </div>
            <p className="field-help">
              长书自动分片，每片分别受此预算限制。全书用量按请求累加，此设置不是总费用上限。
            </p>
            <button className="button primary" disabled={busy > 0}>
              保存模型设置
            </button>
          </form>
        </section>
        <div>
          <section className="card settings-card">
            <h2>项目存储</h2>
            {state.project?.path && <p className="export-path">{state.project.path}</p>}
            <div className="storage-grid">
              <div>
                <span>已存内容对象</span>
                <strong>
                  {bytes(storage.object_bytes ?? storage.objects_bytes ?? storage.used_bytes)}
                </strong>
              </div>
              <div>
                <span>临时空间预留</span>
                <strong>{bytes(storage.reserved_bytes)}</strong>
              </div>
              <div>
                <span>卷剩余空间</span>
                <strong>
                  {storage.free_bytes !== undefined ? bytes(storage.free_bytes) : '待统计'}
                </strong>
              </div>
            </div>
            <p className="muted">
              原件、人工整理稿和已导出成果保留。清理仅处理可重建缓存与已结束任务的临时文件。
            </p>
            <button
              className="button"
              disabled={busy > 0}
              onClick={async () => {
                const data = await run('缓存维护完成。', () => post('/api/maintenance/cache'));
                if (data) setResult(data);
              }}
            >
              整理临时文件与缓存
            </button>
          </section>
          <section className="card settings-card">
            <h2>项目备份</h2>
            <p className="muted">
              保存数据库与所引用的不可变内容对象。备份用于恢复整个项目，包括来源、修订和依据。
            </p>
            <button
              className="button primary"
              disabled={busy > 0}
              onClick={async () => {
                const data = await run('备份任务已提交，进度见任务中心。', () =>
                  post('/api/maintenance/backup'),
                );
                if (data) setResult(data);
              }}
            >
              创建项目备份
            </button>
          </section>
          {result && (
            <section className="card settings-card">
              <h2>最近一次维护</h2>
              <JsonDetails title="查看结果与保存位置" value={result} />
            </section>
          )}
        </div>
      </div>
    </>
  );
}
