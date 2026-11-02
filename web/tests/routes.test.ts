import { NextRequest } from "next/server";
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/core", async (original) => {
  const actual = await original<typeof import("@/lib/core")>();
  return { ...actual, coreRequest: vi.fn(), getDashboard: vi.fn() };
});

import { GET as dashboardGet } from "@/app/api/dashboard/route";
import { POST as loginPost } from "@/app/api/login/route";
import { POST as logoutPost } from "@/app/api/logout/route";
import { DELETE as pinDelete, PUT as pinPut } from "@/app/api/pins/[mac]/route";
import { CoreError, coreRequest, getDashboard } from "@/lib/core";
import { resetRateLimits } from "@/lib/rate-limit";
import { SESSION_COOKIE, createSession, verifySession } from "@/lib/session";

const HOST = "vault.test:8081";
const ORIGIN = `http://${HOST}`;
const MAC = "52:54:00:12:34:56";
const macContext = (mac = MAC) => ({ params: Promise.resolve({ mac: encodeURIComponent(mac) }) });

interface Options {
  method?: string;
  origin?: string | null;
  session?: string;
  body?: unknown;
  headers?: Record<string, string>;
}

function request(path: string, { method = "GET", origin = ORIGIN, session, body, headers = {} }: Options = {}) {
  const allHeaders: Record<string, string> = { host: HOST, ...headers };
  if (origin) allHeaders.origin = origin;
  if (session) allHeaders.cookie = `${SESSION_COOKIE}=${session}`;
  if (body !== undefined) allHeaders["content-type"] = "application/json";
  return new NextRequest(`${ORIGIN}${path}`, {
    method,
    headers: allHeaders,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

const login = (password: string, extra: Options = {}) =>
  loginPost(request("/api/login", { method: "POST", body: { password }, ...extra }));

beforeAll(() => {
  vi.stubEnv("SESSION_SECRET", "unit-test-secret");
  vi.stubEnv("ADMIN_PASSWORD", "correct horse");
  vi.stubEnv("UI_PUBLIC_URL", "");
});

afterAll(() => vi.unstubAllEnvs());

beforeEach(() => {
  resetRateLimits();
  vi.mocked(coreRequest).mockReset();
  vi.mocked(getDashboard).mockReset();
});

describe("sessions", () => {
  it("accepts a token it signed", async () => {
    expect(await verifySession(await createSession())).toBe(true);
  });

  it("rejects missing, tampered and expired tokens", async () => {
    const token = await createSession();
    expect(await verifySession(undefined)).toBe(false);
    expect(await verifySession("nonsense")).toBe(false);
    expect(await verifySession(`${token.slice(0, -1)}${token.endsWith("0") ? "1" : "0"}`)).toBe(false);
    const [, nonce, signature] = token.split(".");
    expect(await verifySession(`99999999999.${nonce}.${signature}`)).toBe(false);
    expect(await verifySession(token, Date.now() + 13 * 3600 * 1000)).toBe(false);
  });

  it("rejects a token signed with another secret", async () => {
    const token = await createSession();
    vi.stubEnv("SESSION_SECRET", "a-different-secret");
    expect(await verifySession(token)).toBe(false);
    vi.stubEnv("SESSION_SECRET", "unit-test-secret");
  });
});

describe("POST /api/login", () => {
  it("sets a session cookie for the right password", async () => {
    const response = await login("correct horse");
    expect(response.status).toBe(200);
    const cookie = response.cookies.get(SESSION_COOKIE);
    expect(cookie?.httpOnly).toBe(true);
    expect(cookie?.sameSite).toBe("lax");
    expect(cookie?.secure).toBeFalsy();
    expect(await verifySession(cookie?.value)).toBe(true);
  });

  it("marks the cookie Secure when UI_PUBLIC_URL is HTTPS", async () => {
    vi.stubEnv("UI_PUBLIC_URL", "https://vault.example.com");
    const response = await login("correct horse", {
      origin: "https://vault.example.com",
      headers: { "x-forwarded-host": "vault.example.com" },
    });
    expect(response.status).toBe(200);
    expect(response.cookies.get(SESSION_COOKIE)?.secure).toBe(true);
    vi.stubEnv("UI_PUBLIC_URL", "");
  });

  it("doesn't mark the cookie Secure on the direct port, even when UI_PUBLIC_URL is HTTPS", async () => {
    vi.stubEnv("UI_PUBLIC_URL", "https://vault.example.com");
    const response = await login("correct horse");
    expect(response.status).toBe(200);
    expect(response.cookies.get(SESSION_COOKIE)?.secure).toBeFalsy();
    vi.stubEnv("UI_PUBLIC_URL", "");
  });

  it("marks the cookie Secure when the proxy says the browser used HTTPS", async () => {
    const response = await login("correct horse", { headers: { "x-forwarded-proto": "https" } });
    expect(response.cookies.get(SESSION_COOKIE)?.secure).toBe(true);
  });

  it("rejects the wrong password without setting a cookie", async () => {
    const response = await login("wrong");
    expect(response.status).toBe(401);
    expect(response.cookies.get(SESSION_COOKIE)).toBeUndefined();
  });

  it("rejects a request from another site, even with the right password", async () => {
    expect((await login("correct horse", { origin: "https://evil.example" })).status).toBe(403);
    expect((await login("correct horse", { origin: null })).status).toBe(403);
  });

  it("blocks a client after repeated failures, even for the right password", async () => {
    const client = { headers: { "x-forwarded-for": "10.0.0.5" } };
    for (let attempt = 0; attempt < 5; attempt += 1) {
      expect((await login("wrong", client)).status).toBe(401);
    }
    expect((await login("correct horse", client)).status).toBe(429);
    expect((await login("correct horse", { headers: { "x-forwarded-for": "10.0.0.6" } })).status).toBe(200);
  });

  it("blocks everyone once overall failures pass the limit, so faked addresses don't help", async () => {
    for (let attempt = 0; attempt < 30; attempt += 1) {
      await login("wrong", { headers: { "x-forwarded-for": `10.1.0.${attempt}` } });
    }
    expect((await login("correct horse", { headers: { "x-forwarded-for": "10.2.0.1" } })).status).toBe(429);
  });

  it("forgets a client's failures after a successful login", async () => {
    const client = { headers: { "x-forwarded-for": "10.0.0.7" } };
    for (let attempt = 0; attempt < 4; attempt += 1) await login("wrong", client);
    expect((await login("correct horse", client)).status).toBe(200);
    for (let attempt = 0; attempt < 4; attempt += 1) {
      expect((await login("wrong", client)).status).toBe(401);
    }
  });

  it("rejects a malformed body", async () => {
    const malformed = new NextRequest(`${ORIGIN}/api/login`, {
      method: "POST",
      headers: { host: HOST, origin: ORIGIN },
      body: "{not json",
    });
    expect((await loginPost(malformed)).status).toBe(400);
  });
});

describe("every route rejects a missing session", () => {
  const routes: [string, (options: Options) => Promise<Response>][] = [
    ["dashboard", (options) => dashboardGet(request("/api/dashboard", options))],
    ["logout", (options) => logoutPost(request("/api/logout", { method: "POST", ...options }))],
    [
      "pin",
      (options) =>
        pinPut(request(`/api/pins/${MAC}`, { method: "PUT", body: { iso_id: "a", mode: "once" }, ...options }), macContext()),
    ],
    ["unpin", (options) => pinDelete(request(`/api/pins/${MAC}`, { method: "DELETE", ...options }), macContext())],
  ];

  it.each(routes)("%s without a session", async (_name, call) => {
    expect((await call({})).status).toBe(401);
    expect(coreRequest).not.toHaveBeenCalled();
    expect(getDashboard).not.toHaveBeenCalled();
  });

  it.each(routes)("%s with a forged session", async (_name, call) => {
    expect((await call({ session: "99999999999.abc.def" })).status).toBe(401);
    expect(coreRequest).not.toHaveBeenCalled();
  });
});

describe("every write rejects another site's request, even with a valid session", () => {
  const writes: [string, (options: Options) => Promise<Response>][] = [
    ["logout", (options) => logoutPost(request("/api/logout", { method: "POST", ...options }))],
    [
      "pin",
      (options) =>
        pinPut(request(`/api/pins/${MAC}`, { method: "PUT", body: { iso_id: "a", mode: "once" }, ...options }), macContext()),
    ],
    ["unpin", (options) => pinDelete(request(`/api/pins/${MAC}`, { method: "DELETE", ...options }), macContext())],
  ];

  it.each(writes)("%s", async (_name, call) => {
    const session = await createSession();
    expect((await call({ session, origin: "https://evil.example" })).status).toBe(403);
    expect((await call({ session, origin: null })).status).toBe(403);
    expect(coreRequest).not.toHaveBeenCalled();
  });
});

describe("origins behind a reverse proxy", () => {
  it("accepts the UI_PUBLIC_URL origin", async () => {
    vi.stubEnv("UI_PUBLIC_URL", "https://vault.example.com");
    vi.mocked(coreRequest).mockResolvedValue({ removed: true });
    const session = await createSession();
    const response = await pinDelete(
      request(`/api/pins/${MAC}`, { method: "DELETE", session, origin: "https://vault.example.com" }),
      macContext(),
    );
    expect(response.status).toBe(200);
    vi.stubEnv("UI_PUBLIC_URL", "");
  });

  it("accepts an origin matching the forwarded host", async () => {
    vi.mocked(coreRequest).mockResolvedValue({ removed: true });
    const session = await createSession();
    const response = await pinDelete(
      request(`/api/pins/${MAC}`, {
        method: "DELETE",
        session,
        origin: "https://proxy.example.com",
        headers: { "x-forwarded-host": "proxy.example.com" },
      }),
      macContext(),
    );
    expect(response.status).toBe(200);
  });
});

describe("pins with a valid session", () => {
  it("passes a valid pin to the core", async () => {
    vi.mocked(coreRequest).mockResolvedValue({ iso_id: "ubuntu-1", mode: "always" });
    const session = await createSession();
    const response = await pinPut(
      request(`/api/pins/${MAC}`, { method: "PUT", session, body: { iso_id: "ubuntu-1", mode: "always", extra: "x" } }),
      macContext(),
    );
    expect(response.status).toBe(200);
    expect(coreRequest).toHaveBeenCalledWith("PUT", `/api/pins/${encodeURIComponent(MAC)}`, {
      iso_id: "ubuntu-1",
      mode: "always",
    });
  });

  it.each([
    ["an invalid MAC", "not-a-mac", { iso_id: "a", mode: "once" }],
    ["a MAC with a path in it", "../../status", { iso_id: "a", mode: "once" }],
    ["a missing ISO", MAC, { mode: "once" }],
    ["an invalid pin type", MAC, { iso_id: "a", mode: "forever" }],
  ])("rejects %s", async (_name, mac, body) => {
    const session = await createSession();
    const response = await pinPut(request("/api/pins/x", { method: "PUT", session, body }), macContext(mac));
    expect(response.status).toBe(400);
    expect(coreRequest).not.toHaveBeenCalled();
  });

  it("passes on a core rejection", async () => {
    vi.mocked(coreRequest).mockRejectedValue(new CoreError("That ISO doesn't exist.", 404));
    const session = await createSession();
    const response = await pinPut(
      request(`/api/pins/${MAC}`, { method: "PUT", session, body: { iso_id: "gone", mode: "once" } }),
      macContext(),
    );
    expect(response.status).toBe(404);
    expect(await response.json()).toEqual({ error: "That ISO doesn't exist." });
  });

  it("reports a core outage as 503", async () => {
    vi.mocked(getDashboard).mockRejectedValue(new CoreError("Can't reach the Iso-Vault core.", 503));
    const session = await createSession();
    const response = await dashboardGet(request("/api/dashboard", { session }));
    expect(response.status).toBe(503);
  });
});
