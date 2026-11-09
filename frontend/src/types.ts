export type RecordData = Record<string, any>;
export type Page = 'sources' | 'books' | 'workspace' | 'review' | 'jobs' | 'exports' | 'settings';
export interface Block extends RecordData {
  id: string;
  type: string;
  text?: string;
  content?: any;
  source_id?: string;
}
export interface Source extends RecordData {
  id: string;
  name?: string;
  status?: string;
  blocks?: Block[];
}
export interface Book extends RecordData {
  id: string;
  title: string;
  author?: string;
}
export interface Revision extends RecordData {
  id: string;
  book_id: string;
  blocks?: Block[];
  metadata?: RecordData;
}
export interface WorkbenchState {
  sources: Source[];
  books: Book[];
  editions: RecordData[];
  branches: RecordData[];
  revisions: Revision[];
  proposals: RecordData[];
  jobs: RecordData[];
  exports: RecordData[];
  review_requests?: RecordData[];
  settings: RecordData;
  storage: RecordData;
  [key: string]: any;
}
export interface SelectedSpan {
  source_id: string;
  block_id: string;
  start?: number;
  end?: number;
  text: string;
  source_name: string;
  type: string;
}
export const emptyState: WorkbenchState = {
  sources: [],
  books: [],
  editions: [],
  branches: [],
  revisions: [],
  proposals: [],
  jobs: [],
  exports: [],
  settings: {},
  storage: {},
  review_requests: [],
};
export const textOf = (block: Block): string => {
  if (typeof block.text === 'string') return block.text;
  if (typeof block.content === 'string') return block.content;
  if (block.content?.text !== undefined) return String(block.content.text);
  if (block.content !== undefined) return JSON.stringify(block.content, null, 2);
  return '';
};
export const fileNameOf = (source: RecordData): string =>
  source.name || source.filename || source.original_name || source.title || source.id;
export const derivationOf = (source: RecordData): string => {
  if (!source.metadata?.derived_from) return '';
  const options = source.metadata.recovery_options || {};
  const action = options.repair && options.repair !== 'none' ? '文本修复' : '重新解码';
  return [action, options.encoding, String(source.id || '').slice(0, 8)]
    .filter(Boolean)
    .join(' · ');
};
export const nameOf = (source: RecordData): string => {
  const derivation = derivationOf(source);
  return fileNameOf(source) + (derivation ? `（${derivation}）` : '');
};
export const spanKey = (span: Pick<SelectedSpan, 'source_id' | 'block_id' | 'start' | 'end'>) =>
  `${span.source_id}:${span.block_id}:${span.start ?? ''}:${span.end ?? ''}`;
export const payloadSpan = ({ source_id, block_id, start, end }: SelectedSpan) => ({
  source_id,
  block_id,
  ...(start === undefined ? {} : { start, end }),
});
export const assemblyPayload = (
  selections: SelectedSpan[],
  editionId = '',
  allowComposite = false,
) => ({
  ...(editionId ? { edition_id: editionId } : {}),
  selections: selections.map(payloadSpan),
  allow_composite: allowComposite,
});
export const jobRecoveryRequest = (
  jobId: string,
  status: string,
  confirmExternal: (message: string) => boolean,
): { path: string; body: RecordData } | null => {
  if (status === 'paused') return { path: `/api/jobs/${jobId}/resume`, body: {} };
  if (status === 'failed') return { path: `/api/jobs/${jobId}/retry`, body: {} };
  if (
    status === 'needs_attention' &&
    confirmExternal('前次请求结果未知，请确认已核实服务端状态；重新请求可能再次计费。')
  )
    return { path: `/api/jobs/${jobId}/retry`, body: { acknowledge_external: true } };
  return null;
};
export const canCancelJob = (status: string) =>
  ['queued', 'running', 'paused', 'failed', 'needs_attention'].includes(status);
export const characterLength = (text: string) => Array.from(text).length;
export const codePointOffset = (text: string, utf16Offset: number) =>
  characterLength(text.slice(0, utf16Offset));
export const encodingOptions = [
  ['auto', '自动探测'],
  ['utf-8', 'UTF-8'],
  ['utf-8-sig', 'UTF-8（BOM）'],
  ['gb18030', 'GB18030 / GBK'],
  ['big5', 'Big5（繁体中文）'],
  ['utf-16', 'UTF-16（BOM）'],
  ['utf-16-le', 'UTF-16 LE'],
  ['utf-16-be', 'UTF-16 BE'],
  ['utf-32', 'UTF-32（BOM）'],
  ['utf-32-le', 'UTF-32 LE'],
  ['utf-32-be', 'UTF-32 BE'],
  ['cp1252', 'Windows-1252'],
  ['latin-1', 'Latin-1'],
].map(([id, label]) => ({ id, label }));
export const textRecoveryPayload = (preview: RecordData) => ({
  encoding: preview.encoding,
  repair: preview.repair,
  expected_blocks_sha: preview.expected_blocks_sha,
});
export const sourceSpansToSelections = (
  spans: RecordData[],
  sources: RecordData[],
): SelectedSpan[] =>
  spans.map((span) => {
    const detail = sources.find((item) => (item.source?.id || item.id) === span.source_id);
    const source = detail?.source || detail;
    const block = (detail?.blocks || source?.blocks || []).find(
      (item: Block) => item.id === span.block_id,
    );
    if (!source || !block) throw new Error('来源范围已无法读取，请重新分析后再选择。');
    const characters = Array.from(textOf(block));
    const start = span.start ?? 0;
    const end = span.end ?? characters.length;
    if (
      !Number.isInteger(start) ||
      !Number.isInteger(end) ||
      start < 0 ||
      end < start ||
      end > characters.length
    )
      throw new Error('来源范围不完整，请重新分析后再选择。');
    return {
      source_id: span.source_id,
      block_id: span.block_id,
      start,
      end,
      text: characters.slice(start, end).join(''),
      source_name: nameOf(source),
      type: block.type,
    };
  });
export const normalizeRecord = (item: RecordData): RecordData => ({
  ...item,
  status: item.status ?? item.state,
  created_at: item.created_at ?? item.created,
});
