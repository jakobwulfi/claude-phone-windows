// The session list behind the status-bar icon: what the claude-phone daemon knows, matched to this window's
// terminals. Kept free of the vscode module so it can be tested with plain node.
import type { TerminalInfo } from './server';

export interface PhoneSession {
  ext: number;
  label: string;
  status: string;
  term_id: string | null;
  /** Where it runs ("VS Code", "Visual Studio", "PowerShell"...). Absent from a daemon older than this field. */
  host?: string | null;
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
  host: string;
  /** This window's terminal running the session; undefined when it runs in another window. */
  terminal?: TerminalInfo;
}

export interface SessionGroup {
  title: string;
  entries: SessionEntry[];
}

const FETCH_TIMEOUT_MS = 2000;
const TERM_PREFIX = 'vscode:';
const ICON = '$(call-outgoing)';
const VS_CODE = 'VS Code';
const THIS_WINDOW = 'This window';
const OTHER_VS_CODE = 'Other VS Code windows';

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

function isOptionalString(value: unknown): boolean {
  return value === undefined || value === null || typeof value === 'string';
}

function isSessionList(body: unknown): body is SessionList {
  if (typeof body !== 'object' || body === null) {
    return false;
  }
  const { sessions, health } = body as Record<string, unknown>;
  return (
    Array.isArray(sessions) &&
    sessions.every(
      (s) =>
        typeof s?.ext === 'number' &&
        typeof s?.label === 'string' &&
        typeof s?.status === 'string' &&
        isOptionalString(s.term_id) &&
        isOptionalString(s.host),
    ) &&
    typeof health === 'object' &&
    health !== null
  );
}

/** The shell pid in a terminal id: `vscode:<pid>`, or `con:<pid>:<time>` for a VS Code terminal the daemon found
 * through its process tree (this window's extension was slow to start). Either way the pid is matched against this
 * window's own terminals, so it never points at another window's. */
export function shellPid(termId: string | null | undefined): number | undefined {
  const match = /^(?:vscode:(\d+)|con:(\d+):\d+)$/.exec(termId ?? '');
  const pid = Number(match?.[1] ?? match?.[2]);
  return Number.isInteger(pid) ? pid : undefined;
}

/** Where a session runs. A daemon that predates the field only knew VS Code terminals. */
function hostOf(s: PhoneSession): string {
  return s.host?.trim() || (s.term_id?.startsWith(TERM_PREFIX) ? VS_CODE : 'Terminal');
}

/** Sessions in extension order, each with this window's terminal when it runs here. */
export function entries(list: SessionList, terminals: TerminalInfo[]): SessionEntry[] {
  return [...list.sessions]
    .sort((a, b) => a.ext - b.ext)
    .map((s) => ({
      ext: s.ext,
      label: s.label,
      status: s.status,
      host: hostOf(s),
      terminal: terminals.find((t) => t.pid === shellPid(s.term_id)),
    }));
}

/** This window's sessions, then the other VS Code windows', then one group per other app (Visual Studio,
 * Windows Terminal, PowerShell...) in alphabetical order. */
export function groups(list: SessionEntry[]): SessionGroup[] {
  const byTitle = new Map<string, SessionEntry[]>();
  for (const entry of list) {
    const title = entry.terminal ? THIS_WINDOW : entry.host === VS_CODE ? OTHER_VS_CODE : entry.host;
    byTitle.set(title, [...(byTitle.get(title) ?? []), entry]);
  }
  const rank = (title: string): number => (title === THIS_WINDOW ? 0 : title === OTHER_VS_CODE ? 1 : 2);
  return [...byTitle.entries()]
    .sort(([a], [b]) => rank(a) - rank(b) || a.localeCompare(b))
    .map(([title, grouped]) => ({ title, entries: grouped }));
}

/** Where to look for a session that isn't in this window, for "<ext> <label> runs in ...". */
export function whereItRuns(entry: SessionEntry): string {
  return entry.host === VS_CODE ? 'another VS Code window' : entry.host;
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
  const exts = list.sessions.map((s) => `${s.ext} ${s.label} (${hostOf(s)})`);
  const sessions = exts.length ? exts.join('\n') : 'No sessions yet: start `claude` in a terminal.';
  return `claude-phone: ${healthLine(list.health)}\n${sessions}\nClick to list them.`;
}
