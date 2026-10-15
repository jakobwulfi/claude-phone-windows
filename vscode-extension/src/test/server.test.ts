import * as assert from 'node:assert/strict';
import { after, before, beforeEach, describe, test } from 'node:test';
import { BridgeServer, MAX_TEXT_CHARS, parseTypeRequest, startBridgeServer, TerminalHost, TerminalInfo } from '../server';

const TOKEN = 'b'.repeat(64);
const SHELL_PID = 4242;

class FakeTerminals implements TerminalHost {
  typed: Array<{ pid: number; text: string }> = [];

  async list(): Promise<TerminalInfo[]> {
    return [{ pid: SHELL_PID, name: 'powershell' }];
  }

  async type(pid: number, text: string): Promise<boolean> {
    if (pid !== SHELL_PID) {
      return false;
    }
    this.typed.push({ pid, text });
    return true;
  }
}

describe('bridge server', () => {
  let server: BridgeServer;
  const terminals = new FakeTerminals();

  before(async () => {
    server = await startBridgeServer(terminals, TOKEN);
  });
  after(() => server.close());
  beforeEach(() => {
    terminals.typed = [];
  });

  function call(path: string, init: RequestInit = {}, token = TOKEN): Promise<Response> {
    return fetch(`http://127.0.0.1:${server.port}${path}`, {
      ...init,
      headers: { Authorization: `Bearer ${token}`, ...(init.headers ?? {}) },
    });
  }

  function typeRequest(body: unknown): Promise<Response> {
    return call('/type', { method: 'POST', body: JSON.stringify(body) });
  }

  test('lists terminals for a request with the token', async () => {
    const res = await call('/terminals');
    assert.equal(res.status, 200);
    assert.deepEqual(await res.json(), { terminals: [{ pid: SHELL_PID, name: 'powershell' }] });
  });

  test('rejects a wrong token', async () => {
    const res = await call('/terminals', {}, 'c'.repeat(64));
    assert.equal(res.status, 401);
  });

  test('rejects requests from a browser page', async () => {
    const res = await call('/terminals', { headers: { Origin: 'https://example.com' } });
    assert.equal(res.status, 403);
  });

  test('types into the terminal with that shell pid', async () => {
    const res = await typeRequest({ pid: SHELL_PID, text: 'ship it' });
    assert.deepEqual(await res.json(), { result: 'sent' });
    assert.deepEqual(terminals.typed, [{ pid: SHELL_PID, text: 'ship it' }]);
  });

  test('reports notfound for a pid no terminal has', async () => {
    const res = await typeRequest({ pid: 999, text: 'hello' });
    assert.deepEqual(await res.json(), { result: 'notfound' });
    assert.deepEqual(terminals.typed, []);
  });

  test('rejects a request without a valid pid', async () => {
    const res = await typeRequest({ pid: 'abc', text: 'hello' });
    assert.equal(res.status, 400);
  });

  test('unknown paths are not found', async () => {
    const res = await call('/nothing');
    assert.equal(res.status, 404);
  });
});

describe('parseTypeRequest', () => {
  test('replaces control characters so nothing but text reaches the terminal', () => {
    const { text } = parseTypeRequest(JSON.stringify({ pid: 1, text: 'yes\u001b[2J\rdo it\n' }));
    assert.equal(text, 'yes [2J do it');
  });

  test('keeps non-ASCII letters', () => {
    assert.equal(parseTypeRequest(JSON.stringify({ pid: 1, text: 'æøå' })).text, 'æøå');
  });

  test('rejects empty and oversized text', () => {
    assert.throws(() => parseTypeRequest(JSON.stringify({ pid: 1, text: '  ' })));
    assert.throws(() => parseTypeRequest(JSON.stringify({ pid: 1, text: 'x'.repeat(MAX_TEXT_CHARS + 1) })));
  });
});
