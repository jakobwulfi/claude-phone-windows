import * as assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { beforeEach, describe, test } from 'node:test';
import { autostart, autostartIn, phoneExeIn } from '../autostart';

describe('reading the setting', () => {
  test('is the top-level autostart, and off when missing or only inside a table', () => {
    assert.equal(autostartIn('owner_name = "Sam"\r\nautostart = true            # start when VS Code opens\r\n'), true);
    assert.equal(autostartIn('autostart = false\n'), false);
    assert.equal(autostartIn('owner_name = "Sam"\n'), false);
    assert.equal(autostartIn('[repo_voices.x]\nautostart = true\n'), false);
  });

  test('phone.exe comes from phone.json, if it is readable', () => {
    assert.equal(phoneExeIn('{"exe": "C:/venv/Scripts/phone.exe"}'), 'C:/venv/Scripts/phone.exe');
    assert.equal(phoneExeIn('{"exe": ""}'), undefined);
    assert.equal(phoneExeIn('not json'), undefined);
  });
});

describe('starting claude-phone when a window opens', () => {
  let home: string;
  let started: string[];
  const start = async (exe: string) => void started.push(exe);

  beforeEach(() => {
    home = fs.mkdtempSync(path.join(os.tmpdir(), 'claude-phone-'));
    started = [];
    fs.writeFileSync(path.join(home, 'phone.json'), JSON.stringify({ exe: 'C:/phone.exe' }));
  });

  test('runs phone up only when autostart is on and the daemon is down', async () => {
    fs.writeFileSync(path.join(home, 'config.toml'), 'autostart = true\n');
    assert.equal(await autostart(home, true, start), 'running');
    assert.equal(await autostart(home, false, start), 'started');
    assert.deepEqual(started, ['C:/phone.exe']);
  });

  test('does nothing when autostart is off or there is no config', async () => {
    assert.equal(await autostart(home, false, start), 'off');
    fs.writeFileSync(path.join(home, 'config.toml'), 'autostart = false\n');
    assert.equal(await autostart(home, false, start), 'off');
    assert.deepEqual(started, []);
  });

  test('says so when it does not know where phone.exe is', async () => {
    fs.writeFileSync(path.join(home, 'config.toml'), 'autostart = true\n');
    fs.rmSync(path.join(home, 'phone.json'));
    assert.equal(await autostart(home, false, start), 'no-phone');
    assert.deepEqual(started, []);
  });
});
