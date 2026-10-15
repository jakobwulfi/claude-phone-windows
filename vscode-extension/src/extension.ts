// claude-phone's VS Code side: a per-window bridge server, advertised through a file in ~/.claude-phone/bridges,
// so the daemon can type phone messages into the Claude Code session running in one of this window's terminals.
import * as crypto from 'node:crypto';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import * as vscode from 'vscode';
import { startBridgeServer, TerminalHost, TerminalInfo } from './server';

// Text, then Enter separately: Claude Code treats a pasted trailing newline as part of the text.
const SUBMIT_DELAY_MS = 300;

function bridgesDir(): string {
  const home = process.env.CLAUDE_PHONE_HOME || path.join(os.homedir(), '.claude-phone');
  return path.join(home, 'bridges');
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
    fs.writeFileSync(file, JSON.stringify({ pid: process.pid, port: server.port, token }), { mode: 0o600 });
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
  status.text = '$(call-outgoing) phone';
  status.tooltip = "claude-phone can type into this window's terminals. Click to list them.";
  status.command = 'claudePhone.showTerminals';
  status.show();

  const showTerminals = vscode.commands.registerCommand('claudePhone.showTerminals', async () => {
    const list = await terminals.list();
    const summary = list.map((t) => `${t.name} (pid ${t.pid})`).join(', ') || 'none';
    vscode.window.showInformationMessage(`claude-phone terminals in this window: ${summary}`);
  });

  context.subscriptions.push(status, showTerminals);
}

export function deactivate(): void {}
