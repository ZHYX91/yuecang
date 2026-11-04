import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import ts from 'typescript';

async function load(name) {
  const source = await readFile(new URL(`../src/${name}`, import.meta.url), 'utf8');
  const compiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  });
  return import(
    `data:text/javascript;base64,${Buffer.from(compiled.outputText).toString('base64')}#${crypto.randomUUID()}`
  );
}

test('browser UTF-16 selections become Python code-point coordinates', async () => {
  const { codePointOffset, characterLength, payloadSpan, spanKey } = await load('types.ts');
  const source = '甲😀乙𝄞丙';
  assert.equal(characterLength(source), 5);
  assert.equal(codePointOffset(source, 3), 2);
  assert.equal(codePointOffset(source, 6), 4);
  const span = {
    source_id: 's1',
    block_id: 'b1',
    start: codePointOffset(source, 1),
    end: codePointOffset(source, 6),
    text: '😀乙𝄞',
    source_name: 'private.md',
    type: 'paragraph',
  };
  assert.deepEqual(payloadSpan(span), { source_id: 's1', block_id: 'b1', start: 1, end: 4 });
  assert.notEqual(spanKey(span), spanKey({ ...span, end: 5 }));
  assert.deepEqual(
    payloadSpan({ source_id: 's1', block_id: 'b2', text: 'body must not be submitted' }),
    { source_id: 's1', block_id: 'b2' },
  );
});

test('composite assembly defaults off and submits only explicit selection with source spans', async () => {
  const { assemblyPayload } = await load('types.ts');
  const spans = [
    {
      source_id: 'source-a',
      block_id: 'block-a',
      start: 2,
      end: 5,
      text: 'private body',
      source_name: 'private.txt',
    },
  ];
  assert.deepEqual(assemblyPayload(spans), {
    selections: [{ source_id: 'source-a', block_id: 'block-a', start: 2, end: 5 }],
    allow_composite: false,
  });
  assert.deepEqual(assemblyPayload(spans, 'edition-b', true), {
    edition_id: 'edition-b',
    selections: [{ source_id: 'source-a', block_id: 'block-a', start: 2, end: 5 }],
    allow_composite: true,
  });
});

test('job recovery distinguishes pause, failure and explicitly acknowledged external retry', async () => {
  const { jobRecoveryRequest, canCancelJob } = await load('types.ts');
  const unexpectedConfirmation = () => {
    throw new Error('unexpected confirmation');
  };
  assert.deepEqual(jobRecoveryRequest('paused-job', 'paused', unexpectedConfirmation), {
    path: '/api/jobs/paused-job/resume',
    body: {},
  });
  assert.deepEqual(jobRecoveryRequest('failed-job', 'failed', unexpectedConfirmation), {
    path: '/api/jobs/failed-job/retry',
    body: {},
  });
  const calls = [];
  const recover = (confirmed) => {
    const request = jobRecoveryRequest('external-job', 'needs_attention', (message) => {
      assert.equal(message, '前次请求结果未知，请确认已核实服务端状态；重新请求可能再次计费。');
      return confirmed;
    });
    if (request) calls.push(request);
  };
  recover(false);
  assert.equal(calls.length, 0);
  recover(true);
  assert.deepEqual(calls, [
    { path: '/api/jobs/external-job/retry', body: { acknowledge_external: true } },
  ]);
  assert.equal(jobRecoveryRequest('done-job', 'succeeded', unexpectedConfirmation), null);
  assert.equal(canCancelJob('failed'), true);
  assert.equal(canCancelJob('needs_attention'), true);
  assert.equal(canCancelJob('succeeded'), false);
  assert.equal(canCancelJob('cancelled'), false);
});

test('API attaches CSRF after session initialization and preserves conflict details', async () => {
  const previous = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (path, init) => {
    calls.push([path, init]);
    return path === '/api/session'
      ? new Response(JSON.stringify({ csrf_token: 'synthetic-token' }), {
          headers: { 'Content-Type': 'application/json' },
        })
      : new Response(
          JSON.stringify({
            error: {
              code: 'revision_conflict',
              message: '版本已变化',
              details: { expected: 'r1', current: 'r2' },
            },
          }),
          { status: 409, headers: { 'Content-Type': 'application/json' } },
        );
  };
  try {
    const { post, ApiError } = await load('api.ts');
    await assert.rejects(
      post('/api/branches/b1/edits', { expected_revision_id: 'r1' }),
      (error) =>
        error instanceof ApiError && error.status === 409 && error.details.current === 'r2',
    );
    assert.equal(calls[1][1].headers.get('X-Yuecang-CSRF'), 'synthetic-token');
    assert.equal(calls[1][1].credentials, 'same-origin');
    assert.equal(calls[1][1].headers.get('Content-Type'), 'application/json');
  } finally {
    globalThis.fetch = previous;
  }
});

test('HTML success and authentication redirects are never acknowledged as saved', async () => {
  const previous = globalThis.fetch;
  let redirected = false;
  globalThis.fetch = async (path) => {
    if (path === '/api/session')
      return new Response(JSON.stringify({ csrf_token: 'synthetic-token' }), {
        headers: { 'Content-Type': 'application/json' },
      });
    return {
      ok: true,
      status: 200,
      redirected,
      headers: new Headers({ 'Content-Type': 'text/html' }),
    };
  };
  try {
    const { post } = await load('api.ts');
    await assert.rejects(post('/api/branches/b1/edits'), /结果格式不正确/);
    redirected = true;
    await assert.rejects(post('/api/branches/b1/edits'), /会话已经改变/);
  } finally {
    globalThis.fetch = previous;
  }
});

test('unstructured server errors ask to inspect existing results before retrying', async () => {
  const previous = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (path) => {
    calls.push(path);
    if (path === '/api/session')
      return new Response(JSON.stringify({ csrf_token: 'synthetic-token' }), {
        headers: { 'Content-Type': 'application/json' },
      });
    return new Response('upstream unavailable', { status: 502 });
  };
  try {
    const { post, ApiError } = await load('api.ts');
    await assert.rejects(post('/api/exports', { revision_id: 'r1' }), (error) => {
      assert.ok(error instanceof ApiError);
      assert.equal(error.status, 502);
      assert.equal(
        error.message,
        '未收到操作结果（502）。请先刷新并核对任务或修改记录，再决定是否重试。',
      );
      return true;
    });
    assert.deepEqual(calls, ['/api/session', '/api/exports']);
  } finally {
    globalThis.fetch = previous;
  }
});
