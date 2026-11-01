import http from "node:http";
import type { ClientState, Dashboard, Iso, Status } from "./types";

const TIMEOUT_MS = 5000;

export class CoreError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

/** Calls the Python core over its Unix socket, which isn't reachable from the network. */
export function coreRequest<T>(method: string, path: string, body?: unknown): Promise<T> {
  const payload = body === undefined ? undefined : JSON.stringify(body);
  return new Promise<T>((resolve, reject) => {
    const request = http.request(
      {
        socketPath: process.env.CORE_SOCKET || "/run/isovault/core.sock",
        path,
        method,
        timeout: TIMEOUT_MS,
        headers: payload
          ? { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(payload) }
          : {},
      },
      (response) => {
        const chunks: Buffer[] = [];
        response.on("data", (chunk: Buffer) => chunks.push(chunk));
        response.on("end", () => {
          const text = Buffer.concat(chunks).toString();
          const status = response.statusCode ?? 500;
          let parsed: unknown;
          try {
            parsed = JSON.parse(text);
          } catch {
            reject(new CoreError("The core sent an unreadable response.", 502));
            return;
          }
          if (status >= 400) {
            const message = (parsed as { error?: string }).error ?? "The core rejected the request.";
            reject(new CoreError(message, status));
            return;
          }
          resolve(parsed as T);
        });
      },
    );
    request.on("timeout", () => request.destroy(new Error("timed out")));
    request.on("error", () => reject(new CoreError("Can't reach the Iso-Vault core.", 503)));
    if (payload) request.write(payload);
    request.end();
  });
}

export async function getDashboard(): Promise<Dashboard> {
  const [status, isos, clients] = await Promise.all([
    coreRequest<Status>("GET", "/api/status"),
    coreRequest<Iso[]>("GET", "/api/isos"),
    coreRequest<ClientState>("GET", "/api/clients"),
  ]);
  return { status, isos, ...clients };
}
