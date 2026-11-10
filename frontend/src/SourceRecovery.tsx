import { useState } from 'react';
import { post } from './api';
import { Issues } from './components';
import { useWorkbench } from './store';
import { encodingOptions, textRecoveryPayload, type RecordData } from './types';

const repairs = [
  ['none', '仅按所选编码重新读取'],
  ['utf8-as-gb18030', '修复 UTF-8 曾被当作 GB18030 读取'],
  ['utf8-as-latin1', '修复 UTF-8 曾被当作 Latin-1 读取'],
  ['utf8-as-cp1252', '修复 UTF-8 曾被当作 Windows-1252 读取'],
  ['nfc', '合并 Unicode 等价组合字符（NFC）'],
  ['remove-invisible', '移除不可见干扰字符'],
];

export default function SourceRecovery({ sourceId }: { sourceId: string }) {
  const { run, busy } = useWorkbench();
  const [encoding, setEncoding] = useState('auto');
  const [repair, setRepair] = useState('none');
  const [preview, setPreview] = useState<RecordData | null>(null);
  const [submitted, setSubmitted] = useState(false);
  const choose = (nextEncoding: string, nextRepair: string) => {
    setEncoding(nextEncoding);
    setRepair(nextRepair);
    setPreview(null);
    setSubmitted(false);
  };
  const inspect = async () => {
    setSubmitted(false);
    setPreview(null);
    const result = await run('', () =>
      post(`/api/sources/${sourceId}/text-preview`, { encoding, repair }),
    );
    if (result) setPreview(result);
  };
  const apply = async () => {
    if (!preview) return;
    const result = await run('已提交新来源任务。原件和原来源保留，完成后可在来源库查看。', () =>
      post(`/api/sources/${sourceId}/text-apply`, textRecoveryPayload(preview)),
    );
    if (result) {
      setSubmitted(true);
      setPreview(null);
    }
  };
  return (
    <details className="source-tools">
      <summary>重新解码与乱码修复</summary>
      <p className="muted">先核对预览，再生成独立新来源。已有归属和整理稿继续引用原来源。</p>
      <div className="option-grid">
        <label>
          重新读取编码
          <select
            aria-label="重新读取编码"
            value={encoding}
            disabled={busy > 0}
            onChange={(event) => choose(event.target.value, repair)}
          >
            {encodingOptions.map((option) => (
              <option key={option.id} value={option.id}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          文本修复方式
          <select
            aria-label="文本修复方式"
            value={repair}
            disabled={busy > 0}
            onChange={(event) => choose(encoding, event.target.value)}
          >
            {repairs.map(([id, label]) => (
              <option key={id} value={id}>
                {label}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="button-group">
        <button className="button small" disabled={busy > 0} onClick={() => void inspect()}>
          预览处理结果
        </button>
        <button
          className="button small accent"
          disabled={busy > 0 || !preview || !preview.changed}
          onClick={() => void apply()}
        >
          确认生成新来源
        </button>
      </div>
      {submitted && (
        <p className="inline-banner">处理已提交，可在任务中心查看结果；新来源不会替换当前来源。</p>
      )}
      {preview && (
        <div className="recovery-preview">
          <p className="field-help">
            预览编码：{preview.encoding} · 共 {Number(preview.chars || 0).toLocaleString()} 字符
            {preview.truncated ? ' · 以下仅显示开头部分，请结合原件核对' : ''}
          </p>
          <Issues issues={preview.issues} />
          <div className="diff-grid">
            <label>
              当前来源文字
              <textarea
                aria-label="修复前预览"
                rows={6}
                readOnly
                value={preview.original_preview || ''}
              />
            </label>
            <label>
              处理后文字
              <textarea aria-label="修复后预览" rows={6} readOnly value={preview.preview || ''} />
            </label>
          </div>
          {!preview.changed && (
            <p className="field-help">当前处理没有改变内容，无需创建重复来源。</p>
          )}
          {(preview.encoding_candidates?.length > 0 || preview.repair_candidates?.length > 0) && (
            <details className="recovery-alternatives">
              <summary>其他编码与修复候选</summary>
              {(preview.encoding_candidates || []).map((candidate: RecordData) => (
                <article key={candidate.encoding} className="candidate-preview">
                  <div className="button-group">
                    <strong>{candidate.encoding}</strong>
                    <button
                      className="button small"
                      disabled={busy > 0}
                      onClick={() => choose(candidate.encoding, 'none')}
                    >
                      选择此编码，再预览
                    </button>
                  </div>
                  <pre>{candidate.preview}</pre>
                </article>
              ))}
              {(preview.repair_candidates || []).map((candidate: RecordData) => (
                <article key={candidate.id} className="candidate-preview">
                  <div className="button-group">
                    <strong>{candidate.label}</strong>
                    <button
                      className="button small"
                      disabled={busy > 0}
                      onClick={() => choose(preview.encoding, candidate.id)}
                    >
                      选择此修复，再预览
                    </button>
                  </div>
                  <pre>{candidate.preview}</pre>
                </article>
              ))}
            </details>
          )}
        </div>
      )}
    </details>
  );
}
