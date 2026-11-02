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
export const nameOf = (source: RecordData): string =>
  source.name || source.filename || source.original_name || source.title || source.id;
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
export const normalizeRecord = (item: RecordData): RecordData => ({
  ...item,
  status: item.status ?? item.state,
  created_at: item.created_at ?? item.created,
});
