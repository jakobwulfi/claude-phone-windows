// The session list behind the status-bar icon: what the claude-phone daemon knows, matched to this window's
// terminals. Kept free of the vscode module so it can be tested with plain node.
import type { TerminalInfo } from './server';

export interface PhoneSession {
  ext: number;
  label: string;
  status: string;
  term_id: string | null;
}

export interface PhoneHealth {
  freeswitch: boolean;
  desk_online: boolean;
  ring_ext: string;
  ip_warning: string | null;
}

export interface SessionList {
  sessions: PhoneSession[];
  health: PhoneHealth;
}

export interface SessionEntry {
  ext: number;
  label: string;
  status: string;
  /** This window's terminal running the session; undefined when it runs in another window. */
  terminal?: TerminalInfo;
}

const FETCH_TIMEOUT_MS = 2000;
const TERM_PREFIX = 'vscode:';
const ICON = '$(call-outgoing)';

/** The daemon's session list, or undefined when the daemon isn't running or answers with something else. */
export async function fetchSessions(port: number, timeoutMs = FETCH_TIMEOUT_MS): Promise<SessionList | undefined> {
  try {
    const res = await fetch(`http://127.0.0.1:${port}/sessions`, { signal: AbortSignal.timeout(timeoutMs) });
    if (!res.ok) {
      return undefined;
    }
    const body: unknown = await res.json();
    return isSessionList(body) ? body : undefined;
  } catch {
    return undefined;
  }
}

function isSessionList(body: unknown): body is SessionList {
  if (typeof body !== 'object' || body === null) {
    return false;
  }
  const { sessions, health } = body as Record<string, unknown>;
  return (
    Array.isArray(sessions) &&
    sessions.every((s) => typeof s?.ext === 'number' && typeof s?.label === 'string') &&
    typeof health === 'object' &&
    health !== null
  );
}

export function shellPid(termId: string | null): number | undefined {
  if (!termId?.startsWith(TERM_PREFIX)) {
    return undefined;
  }
  const pid = Number(termId.slice(TERM_PREFIX.length));
  return Number.isInteger(pid) ? pid : undefined;
}

/** Sessions in extension order, each with this window's terminal when it runs here. */
export function entries(list: SessionList, terminals: TerminalInfo[]): SessionEntry[] {
  return [...list.sessions]
    .sort((a, b) => a.ext - b.ext)
    .map((s) => ({
      ext: s.ext,
      label: s.label,
      status: s.status,
      terminal: terminals.find((t) => t.pid === shellPid(s.term_id)),
    }));
}

/** One line on the phone system: FreeSWITCH, the phone that rings, and an address warning if any. */
export function healthLine(health: PhoneHealth): string {
  const parts = health.freeswitch
    ? [`phone ${health.ring_ext} ${health.desk_online ? 'registered' : 'not registered'}`]
    : ['FreeSWITCH not connected'];
  if (health.ip_warning) {
    parts.push('address changed, see phone status');
  }
  return parts.join(' · ');
}

export function statusText(list: SessionList | undefined): string {
  return list ? `${ICON} ${list.sessions.length}` : `${ICON} off`;
}

export function statusTooltip(list: SessionList | undefined): string {
  if (!list) {
    return "claude-phone isn't running. Start it with /claude-phone in Claude Code, or `phone up`.";
  }
  const exts = list.sessions.map((s) => `${s.ext} ${s.label}`);
  const sessions = exts.length ? exts.join('\n') : 'No sessions yet: start `claude` in a terminal.';
  return `claude-phone: ${healthLine(list.health)}\n${sessions}\nClick to list them.`;
}
