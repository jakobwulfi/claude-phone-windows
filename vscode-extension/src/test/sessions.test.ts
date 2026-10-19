import * as assert from 'node:assert/strict';
import * as http from 'node:http';
import type { AddressInfo } from 'node:net';
import { after, before, describe, test } from 'node:test';
import {
  entries,
  fetchSessions,
  groups,
  healthLine,
  SessionList,
  shellPid,
  statusText,
  statusTooltip,
  whereItRuns,
} from '../sessions';

const HEALTH = { freeswitch: true, desk_online: true, ring_ext: '201', ip_warning: null };

const LIST: SessionList = {
  sessions: [
    { ext: 302, label: 'site-fe/main', status: 'working', term_id: 'vscode:200', host: 'VS Code' },
    { ext: 301, label: 'claude-phone-windows/phase-6', status: 'idle', term_id: 'vscode:100', host: 'VS Code' },
  ],
  health: HEALTH,
};

const MIXED: SessionList = {
  sessions: [
    { ext: 301, label: 'api/main', status: 'idle', term_id: 'vscode:100', host: 'VS Code' },
    { ext: 302, label: 'api/fix', status: 'idle', term_id: 'vscode:200', host: 'VS Code' },
    { ext: 303, label: 'site/main', status: 'working', term_id: 'con:10:1', host: 'Visual Studio' },
    { ext: 304, label: 'tools/main', status: 'idle', term_id: 'con:20:2', host: 'PowerShell' },
    { ext: 305, label: 'site/dev', status: 'idle', term_id: 'con:30:3', host: 'Visual Studio' },
    { ext: 306, label: 'docs/main', status: 'idle', term_id: 'con:40:4', host: 'Windows Terminal' },
  ],
  health: HEALTH,
};

describe('session entries', () => {
  test('are in extension order, with the terminal when it is in this window', () => {
    const result = entries(LIST, [{ pid: 100, name: 'claude' }]);

    assert.deepEqual(result, [
      {
        ext: 301,
        label: 'claude-phone-windows/phase-6',
        status: 'idle',
        host: 'VS Code',
        terminal: { pid: 100, name: 'claude' },
      },
      { ext: 302, label: 'site-fe/main', status: 'working', host: 'VS Code', terminal: undefined },
    ]);
  });

  test('from a daemon that sends no host are VS Code sessions, as they all were', () => {
    const old: SessionList = {
      sessions: [{ ext: 301, label: 'a/main', status: 'idle', term_id: 'vscode:1' }],
      health: HEALTH,
    };
    assert.equal(entries(old, [])[0].host, 'VS Code');
    assert.equal(entries({ ...old, sessions: [{ ...old.sessions[0], term_id: null }] }, [])[0].host, 'Terminal');
  });

  test('in a console are matched to this window only when its shell is one of this window\'s terminals', () => {
    const result = entries(MIXED, [{ pid: 10, name: 'powershell' }]);
    assert.deepEqual(result.find((e) => e.ext === 303)?.terminal, { pid: 10, name: 'powershell' });
    assert.equal(result.find((e) => e.ext === 304)?.terminal, undefined);
  });

  test('with a blank host fall back to where the terminal id says', () => {
    const blank: SessionList = {
      sessions: [{ ext: 301, label: 'a/main', status: 'idle', term_id: 'vscode:1', host: '  ' }],
      health: HEALTH,
    };
    assert.equal(entries(blank, [])[0].host, 'VS Code');
  });

  test('are empty when there are no sessions', () => {
    assert.deepEqual(entries({ sessions: [], health: HEALTH }, []), []);
  });

  test('shell pid comes from a VS Code terminal id or a console id, and from nothing else', () => {
    assert.equal(shellPid('vscode:4242'), 4242);
    assert.equal(shellPid('con:4242:1760900000000'), 4242);
    for (const odd of ['vscode:abc', 'vscode:12:3', 'con:12', 'con:x:1', 'tab', '', null, undefined]) {
      assert.equal(shellPid(odd), undefined, String(odd));
    }
  });
});

describe('session groups', () => {
  const titles = (g: ReturnType<typeof groups>) => g.map((x) => [x.title, x.entries.map((e) => e.ext)]);

  test('are this window, the other VS Code windows, then one per other app', () => {
    const result = groups(entries(MIXED, [{ pid: 100, name: 'claude' }]));

    assert.deepEqual(titles(result), [
      ['This window', [301]],
      ['Other VS Code windows', [302]],
      ['PowerShell', [304]],
      ['Visual Studio', [303, 305]],
      ['Windows Terminal', [306]],
    ]);
  });

  test('leave out groups with no sessions', () => {
    assert.deepEqual(titles(groups(entries(LIST, []))), [['Other VS Code windows', [301, 302]]]);
    assert.deepEqual(groups([]), []);
  });

  test('keep a VS Code terminal the daemon found by its process tree in this window', () => {
    const late: SessionList = {
      sessions: [{ ext: 301, label: 'a/main', status: 'idle', term_id: 'con:100:5', host: 'VS Code' }],
      health: HEALTH,
    };
    assert.deepEqual(titles(groups(entries(late, [{ pid: 100, name: 'claude' }]))), [['This window', [301]]]);
  });

  test('put a session of unknown host under Terminal, and sort the hosts regardless of case', () => {
    const odd: SessionList = {
      sessions: [
        { ext: 301, label: 'a/main', status: 'idle', term_id: null },
        { ext: 302, label: 'b/main', status: 'idle', term_id: 'con:1:1', host: 'cmd shell' },
        { ext: 303, label: 'c/main', status: 'idle', term_id: 'con:2:2', host: 'Zsh' },
      ],
      health: HEALTH,
    };
    const result = groups(entries(odd, []));
    assert.deepEqual(titles(result), [['cmd shell', [302]], ['Terminal', [301]], ['Zsh', [303]]]);
    assert.equal(whereItRuns(result[1].entries[0]), 'Terminal');
  });

  test('say where a session outside this window runs', () => {
    const [vsCode, vs] = entries({ ...MIXED, sessions: [MIXED.sessions[1], MIXED.sessions[2]] }, []);
    assert.equal(whereItRuns(vsCode), 'another VS Code window');
    assert.equal(whereItRuns(vs), 'Visual Studio');
  });
});

describe('status', () => {
  test('shows the session count, or off when the daemon is down', () => {
    assert.equal(statusText(LIST), '$(call-outgoing) 2');
    assert.equal(statusText(undefined), '$(call-outgoing) off');
  });

  test('tooltip lists the extensions with where they run, or says how to start claude-phone', () => {
    assert.match(statusTooltip(LIST), /302 site-fe\/main \(VS Code\)/);
    assert.match(statusTooltip(MIXED), /303 site\/main \(Visual Studio\)/);
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
    // A field of the wrong type would throw later, in the status bar's refresh: refuse the whole answer instead.
    const wrong = [{ host: 7 }, { term_id: 12 }, { term_id: {} }, { status: undefined }];
    for (const change of wrong) {
      const session = { ext: 301, label: 'a', status: 'idle', term_id: 'vscode:1', ...change };
      reply = { status: 200, body: { ...LIST, sessions: [session] } };
      assert.equal(await fetchSessions(port), undefined, JSON.stringify(change));
    }
  });

  test('accepts sessions with a null host or none at all', async () => {
    const sessions = [
      { ext: 301, label: 'a/main', status: 'idle', term_id: 'vscode:1', host: null },
      { ext: 302, label: 'b/main', status: 'idle', term_id: 'vscode:2' },
    ];
    reply = { status: 200, body: { sessions, health: HEALTH } };
    assert.equal((await fetchSessions(port))?.sessions.length, 2);
  });

  test('is undefined when nothing is listening', async () => {
    const closed = http.createServer();
    await new Promise<void>((resolve) => closed.listen(0, '127.0.0.1', resolve));
    const freePort = (closed.address() as AddressInfo).port;
    await new Promise<void>((resolve) => closed.close(() => resolve()));
    assert.equal(await fetchSessions(freePort), undefined);
  });
});
