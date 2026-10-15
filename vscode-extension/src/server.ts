// The bridge server: lets the claude-phone daemon list this window's terminals and type into one of them.
// Localhost only, and every request must carry the window's random token.
import * as crypto from 'node:crypto';
import * as http from 'node:http';
import type { AddressInfo } from 'node:net';

export interface TerminalInfo {
  pid: number;
  name: string;
}

export interface TerminalHost {
  list(): Promise<TerminalInfo[]>;
  /** Types the text and submits it. False when no terminal has that shell pid. */
  type(pid: number, text: string): Promise<boolean>;
}

export interface BridgeServer {
  port: number;
  close(): void;
}

const MAX_BODY_BYTES = 16 * 1024;
export const MAX_TEXT_CHARS = 4000;
// Control characters (escape sequences, carriage returns) could do more in a terminal than type text.
const CONTROL_CHARS = /[\u0000-\u001f\u007f-\u009f]/g;

class HttpError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
  }
}

export function startBridgeServer(host: TerminalHost, token: string): Promise<BridgeServer> {
  const server = http.createServer((req, res) => {
    const { port } = server.address() as AddressInfo;
    handle(req, host, token, port).then(
      (body) => send(res, 200, body),
      (err: unknown) => {
        if (err instanceof HttpError) {
          send(res, err.status, { error: err.message });
        } else {
          send(res, 500, { error: 'internal error' });
        }
      },
    );
  });
  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address() as AddressInfo;
      resolve({ port, close: () => server.close() });
    });
  });
}

async function handle(req: http.IncomingMessage, host: TerminalHost, token: string, port: number): Promise<object> {
  // A browser always sends Origin on cross-site requests, and a rebound DNS name shows up in Host.
  if (req.headers.origin !== undefined) {
    throw new HttpError(403, 'browser requests are not allowed');
  }
  if (req.headers.host !== `127.0.0.1:${port}`) {
    throw new HttpError(403, 'unexpected host');
  }
  if (!isAuthorized(req.headers.authorization, token)) {
    throw new HttpError(401, 'missing or wrong token');
  }
  if (req.method === 'GET' && req.url === '/terminals') {
    return { terminals: await host.list() };
  }
  if (req.method === 'POST' && req.url === '/type') {
    const { pid, text } = parseTypeRequest(await readBody(req));
    return { result: (await host.type(pid, text)) ? 'sent' : 'notfound' };
  }
  throw new HttpError(404, 'not found');
}

function isAuthorized(header: string | undefined, token: string): boolean {
  const expected = Buffer.from(`Bearer ${token}`);
  const given = Buffer.from(header ?? '');
  return given.length === expected.length && crypto.timingSafeEqual(given, expected);
}

export function parseTypeRequest(raw: string): { pid: number; text: string } {
  let body: unknown;
  try {
    body = JSON.parse(raw);
  } catch {
    throw new HttpError(400, 'body must be JSON');
  }
  if (typeof body !== 'object' || body === null) {
    throw new HttpError(400, 'body must be an object');
  }
  const { pid, text } = body as Record<string, unknown>;
  if (typeof pid !== 'number' || !Number.isInteger(pid) || pid <= 0) {
    throw new HttpError(400, 'pid must be a positive integer');
  }
  if (typeof text !== 'string') {
    throw new HttpError(400, 'text must be a string');
  }
  const clean = text.replace(CONTROL_CHARS, ' ').trim();
  if (clean.length === 0 || clean.length > MAX_TEXT_CHARS) {
    throw new HttpError(400, `text must be 1 to ${MAX_TEXT_CHARS} characters`);
  }
  return { pid, text: clean };
}

function readBody(req: http.IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    let size = 0;
    req.on('data', (chunk: Buffer) => {
      size += chunk.length;
      if (size > MAX_BODY_BYTES) {
        reject(new HttpError(413, 'body too large'));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    req.on('error', reject);
  });
}

function send(res: http.ServerResponse, status: number, body: object): void {
  res.writeHead(status, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify(body));
}
