import { NextResponse, type NextRequest } from "next/server";
import { CoreError } from "./core";
import { SESSION_COOKIE, verifySession } from "./session";

export function jsonError(message: string, status: number): NextResponse {
  return NextResponse.json({ error: message }, { status });
}

/**
 * Accepts requests from the UI's own address, as set in UI_PUBLIC_URL or as the browser reached it.
 * Another site can't forge either, and a rebound DNS name wouldn't carry the session cookie.
 */
export function originAllowed(request: NextRequest): boolean {
  const origin = request.headers.get("origin");
  if (!origin) return false;
  let parsed: URL;
  try {
    parsed = new URL(origin);
  } catch {
    return false;
  }
  const publicUrl = process.env.UI_PUBLIC_URL;
  if (publicUrl) {
    try {
      if (new URL(publicUrl).origin === parsed.origin) return true;
    } catch {
      // A malformed UI_PUBLIC_URL falls through to the host comparison.
    }
  }
  const hosts = [request.headers.get("host"), request.headers.get("x-forwarded-host")];
  return hosts.some((host) => host !== null && host === parsed.host);
}

/** Checked in every route handler as well as proxy.ts, so routes never rely on the proxy alone. */
export async function requireSession(request: NextRequest): Promise<NextResponse | null> {
  const valid = await verifySession(request.cookies.get(SESSION_COOKIE)?.value);
  return valid ? null : jsonError("Please log in.", 401);
}

export async function requireWriteAccess(request: NextRequest): Promise<NextResponse | null> {
  if (!originAllowed(request)) return jsonError("This request came from another site.", 403);
  return requireSession(request);
}

/**
 * Whether this request reached us over HTTPS. A browser drops a Secure cookie sent over plain HTTP,
 * so someone using the direct port must get a normal cookie even when UI_PUBLIC_URL is HTTPS.
 */
export function usesHttps(request: NextRequest): boolean {
  if (request.headers.get("x-forwarded-proto") === "https") return true;
  const publicUrl = process.env.UI_PUBLIC_URL;
  if (!publicUrl?.startsWith("https:")) return false;
  const host = request.headers.get("x-forwarded-host") ?? request.headers.get("host");
  try {
    return new URL(publicUrl).host === host;
  } catch {
    return false;
  }
}

export function coreErrorResponse(error: unknown): NextResponse {
  if (error instanceof CoreError) return jsonError(error.message, error.status);
  console.error(error);
  return jsonError("Something went wrong.", 500);
}
