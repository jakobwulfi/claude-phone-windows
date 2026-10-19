// claude-phone's VS Code side: a per-window bridge server, advertised through a file in ~/.claude-phone/bridges,
// so the daemon can type phone messages into the Claude Code session running in one of this window's terminals.
import * as crypto from 'node:crypto';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import * as vscode from 'vscode';
import { autostart } from './autostart';
import { startBridgeServer, TerminalHost, TerminalInfo } from './server';
import {
  entries,
  fetchSessions,
  groups,
  healthLine,
  SessionEntry,
  SessionList,
  statusText,
  statusTooltip,
  whereItRuns,
} from './sessions';

// Text, then Enter separately: Claude Code treats a pasted trailing newline as part of the text.
const SUBMIT_DELAY_MS = 300;
const REFRESH_MS = 15_000;
const DEFAULT_DAEMON_PORT = 8765;

function phoneHome(): string {
  return process.env.CLAUDE_PHONE_HOME || path.join(os.homedir(), '.claude-phone');
}

function bridgesDir(): string {
  return path.join(phoneHome(), 'bridges');
}

/** With autostart on, starts claude-phone when it isn't running. The status bar's next refresh shows it. */
async function startIfWanted(daemonRunning: boolean): Promise<void> {
  try {
    if ((await autostart(phoneHome(), daemonRunning)) === 'no-phone') {
      vscode.window.showWarningMessage(
        "claude-phone's autostart is on, but VS Code doesn't know where phone.exe is yet: run `phone up` once.");
    }
  } catch (err: unknown) {
    const reason = err instanceof Error ? err.message : String(err);
    vscode.window.showWarningMessage(`claude-phone's autostart couldn't run \`phone up\`: ${reason}`);
  }
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

class VsCodeTerminals implements TerminalHost {
  async list(): Promise<TerminalInfo[]> {
    const terminals = await Promise.all(
      vscode.window.terminals.map(async (t) => ({ pid: await t.processId, name: t.name })),
    );
    return terminals.filter((t): t is TerminalInfo => typeof t.pid === 'number');
  }

  async type(pid: number, text: string): Promise<boolean> {
    const terminal = await this.find(pid);
    if (!terminal) {
      return false;
    }
    terminal.sendText(text, false);
    await delay(SUBMIT_DELAY_MS);
    terminal.sendText('\r', false);
    return true;
  }

  /** Brings the terminal forward. False when it isn't in this window. */
  async show(pid: number): Promise<boolean> {
    const terminal = await this.find(pid);
    terminal?.show();
    return terminal !== undefined;
  }

  private async find(pid: number): Promise<vscode.Terminal | undefined> {
    for (const terminal of vscode.window.terminals) {
      if ((await terminal.processId) === pid) {
        return terminal;
      }
    }
    return undefined;
  }
}

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  const terminals = new VsCodeTerminals();
  const token = crypto.randomBytes(32).toString('hex');
  const file = path.join(bridgesDir(), `${vscode.env.sessionId}.json`);
  try {
    const server = await startBridgeServer(terminals, token);
    fs.mkdirSync(path.dirname(file), { recursive: true });
    // app: which editor this is (VS Code, Cursor, Windsurf, VSCodium, Insiders), for whoever reads the file.
    const bridge = { pid: process.pid, port: server.port, token, app: vscode.env.appName };
    fs.writeFileSync(file, JSON.stringify(bridge), { mode: 0o600 });
    context.subscriptions.push({
      dispose: () => {
        server.close();
        fs.rmSync(file, { force: true });
      },
    });
  } catch (err: unknown) {
    const reason = err instanceof Error ? err.message : String(err);
    vscode.window.showErrorMessage(`claude-phone couldn't start, so the phone can't reach this window: ${reason}`);
    return;
  }

  const status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 0);
  status.command = 'claudePhone.showSessions';
  status.show();

  const refresh = async (): Promise<SessionList | undefined> => {
    const list = await fetchSessions(daemonPort());
    status.text = statusText(list);
    status.tooltip = statusTooltip(list);
    return list;
  };
  void refresh().then((list) => startIfWanted(list !== undefined));
  const timer = setInterval(() => void refresh(), REFRESH_MS);

  const showSessions = vscode.commands.registerCommand('claudePhone.showSessions', async () => {
    const list = await refresh();
    if (!list) {
      vscode.window.showWarningMessage(statusTooltip(undefined));
      return;
    }
    const picked = await vscode.window.showQuickPick(quickPickItems(entries(list, await terminals.list())), {
      title: `claude-phone · ${healthLine(list.health)}`,
      placeHolder: list.sessions.length ? 'Dial an extension to reach its session. Pick one to show its terminal.'
        : 'No sessions yet: start `claude` in a terminal.',
    });
    if (!picked?.entry) {
      return;
    }
    const { entry } = picked;
    if (!entry.terminal || !(await terminals.show(entry.terminal.pid))) {
      vscode.window.showInformationMessage(`${entry.ext} ${entry.label} runs in ${whereItRuns(entry)}.`);
    }
  });

  context.subscriptions.push(status, showSessions, { dispose: () => clearInterval(timer) });
}

interface SessionPickItem extends vscode.QuickPickItem {
  entry?: SessionEntry;
}

function quickPickItems(list: SessionEntry[]): SessionPickItem[] {
  const toItem = (entry: SessionEntry): SessionPickItem => ({
    label: `${entry.ext}  ${entry.label}`,
    description: `${entry.status} · ${entry.host}`,
    detail: entry.terminal ? `Terminal "${entry.terminal.name}"` : undefined,
    entry,
  });
  return groups(list).flatMap(({ title, entries: grouped }) => {
    const separator: SessionPickItem = { label: title, kind: vscode.QuickPickItemKind.Separator };
    return [separator, ...grouped.map(toItem)];
  });
}

function daemonPort(): number {
  return vscode.workspace.getConfiguration('claudePhone').get<number>('daemonPort', DEFAULT_DAEMON_PORT);
}

export function deactivate(): void {}
