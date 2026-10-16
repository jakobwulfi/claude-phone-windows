// autostart: when it is on in config.toml, a window that opens while claude-phone is down runs `phone up`.
// phone.exe's path comes from phone.json, which `phone up` and `phone config set autostart on` leave behind.
// Kept free of the vscode module so it can be tested with plain node.
import { spawn } from 'node:child_process';
import * as fs from 'node:fs';
import * as path from 'node:path';

/** The top-level `autostart` in config.toml. `phone config` writes the plain keys before any [table]. */
export function autostartIn(toml: string): boolean {
  for (const line of toml.split(/\r?\n/)) {
    if (line.trimStart().startsWith('[')) {
      break;
    }
    const m = /^\s*autostart\s*=\s*(true|false)\b/.exec(line);
    if (m) {
      return m[1] === 'true';
    }
  }
  return false;
}

/** phone.exe from phone.json, or undefined when the file is missing or unreadable. */
export function phoneExeIn(json: string): string | undefined {
  try {
    const exe: unknown = JSON.parse(json)?.exe;
    return typeof exe === 'string' && exe ? exe : undefined;
  } catch {
    return undefined;
  }
}

function readOr(file: string, fallback: string): string {
  try {
    return fs.readFileSync(file, 'utf8');
  } catch {
    return fallback;
  }
}

export type Start = (exe: string) => Promise<void>;
export type Outcome = 'off' | 'running' | 'no-phone' | 'started';

/** Runs `phone up` in the background. Resolves once it has started, rejects if it can't. */
export const startPhone: Start = (exe) =>
  new Promise((resolve, reject) => {
    const child = spawn(exe, ['up'], { detached: true, windowsHide: true, stdio: 'ignore' });
    child.once('error', reject);
    child.once('spawn', () => {
      child.unref();
      resolve();
    });
  });

/** Starts claude-phone if autostart is on and the daemon didn't answer. */
export async function autostart(home: string, daemonRunning: boolean, start: Start = startPhone): Promise<Outcome> {
  if (!autostartIn(readOr(path.join(home, 'config.toml'), ''))) {
    return 'off';
  }
  if (daemonRunning) {
    return 'running';
  }
  const exe = phoneExeIn(readOr(path.join(home, 'phone.json'), ''));
  if (!exe) {
    return 'no-phone';
  }
  await start(exe);
  return 'started';
}
