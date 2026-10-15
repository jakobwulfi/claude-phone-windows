import * as assert from 'node:assert/strict';
import * as http from 'node:http';
import type { AddressInfo } from 'node:net';
import { after, before, describe, test } from 'node:test';
import { entries, fetchSessions, healthLine, SessionList, shellPid, statusText, statusTooltip } from '../sessions';

const HEALTH = { freeswitch: true, desk_online: true, ring_ext: '201', ip_warning: null };

const LIST: SessionList = {
  sessions: [
    { ext: 302, label: 'site-fe/main', status: 'working', term_id: 'vscode:200' },
    { ext: 301, label: 'claude-phone-windows/phase-6', status: 'idle', term_id: 'vscode:100' },
  ],
  health: HEALTH,
};

describe('session entries', () => {
  test('are in extension order, with the terminal when it is in this window', () => {
    const result = entries(LIST, [{ pid: 100, name: 'claude' }]);

    assert.deepEqual(result, [
      { ext: 301, label: 'claude-phone-windows/phase-6', status: 'idle', terminal: { pid: 100, name: 'claude' } },
      { ext: 302, label: 'site-fe/main', status: 'working', terminal: undefined },
    ]);
  });

  test('are empty when there are no sessions', () => {
    assert.deepEqual(entries({ sessions: [], health: HEALTH }, []), []);
  });

  test('shell pid comes only from a VS Code terminal id', () => {
    assert.equal(shellPid('vscode:4242'), 4242);
    assert.equal(shellPid('vscode:abc'), undefined);
    assert.equal(shellPid('tab'), undefined);
    assert.equal(shellPid(null), undefined);
  });
});

describe('status', () => {
  test('shows the session count, or off when the daemon is down', () => {
    assert.equal(statusText(LIST), '$(call-outgoing) 2');
    assert.equal(statusText(undefined), '$(call-outgoing) off');
  });

  test('tooltip lists the extensions, or says how to start claude-phone', () => {
    assert.match(statusTooltip(LIST), /302 site-fe\/main/);
    assert.match(statusTooltip(undefined), /\/claude-phone/);
  });

  test('health line names the phone and whether it is registered', () => {
    assert.equal(healthLine(HEALTH), 'phone 201 registered');
    assert.equal(healthLine({ ...HEALTH, desk_online: false }), 'phone 201 not registered');
    assert.equal(healthLine({ ...HEALTH, freeswitch: false }), 'FreeSWITCH not connected');
    assert.equal(healthLine({ ...HEALTH, ip_warning: 'moved' }), 'phone 201 registered · address changed, see phone status');
  });
});

describe('fetching the session list', () => {
  let server: http.Server;
  let port: number;
  let reply: { status: number; body: unknown };

  before(async () => {
    server = http.createServer((req, res) => {
      assert.equal(req.url, '/sessions');
      res.writeHead(reply.status, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(reply.body));
    });
    await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
    port = (server.address() as AddressInfo).port;
  });
  after(() => server.close());

  test('returns what the daemon sends', async () => {
    reply = { status: 200, body: LIST };
    assert.deepEqual(await fetchSessions(port), LIST);
  });

  test('is undefined when the daemon answers with an error or the wrong shape', async () => {
    reply = { status: 403, body: {} };
    assert.equal(await fetchSessions(port), undefined);
    reply = { status: 200, body: { sessions: 'nope' } };
    assert.equal(await fetchSessions(port), undefined);
  });

  test('is undefined when nothing is listening', async () => {
    const closed = http.createServer();
    await new Promise<void>((resolve) => closed.listen(0, '127.0.0.1', resolve));
    const freePort = (closed.address() as AddressInfo).port;
    await new Promise<void>((resolve) => closed.close(() => resolve()));
    assert.equal(await fetchSessions(freePort), undefined);
  });
});
