import { NextResponse, type NextRequest } from "next/server";
import { jsonError, originAllowed, usesHttps } from "@/lib/guard";
import { clearFailedLogins, loginBlocked, recordFailedLogin } from "@/lib/rate-limit";
import { SESSION_COOKIE, SESSION_SECONDS, createSession, passwordMatches } from "@/lib/session";

export async function POST(request: NextRequest) {
  if (!originAllowed(request)) return jsonError("This request came from another site.", 403);

  const client = request.headers.get("x-forwarded-for")?.split(",")[0]?.trim() || "unknown";
  if (loginBlocked(client)) return jsonError("Too many attempts. Wait a minute, then try again.", 429);

  let password: unknown;
  try {
    ({ password } = await request.json());
  } catch {
    return jsonError("Send a JSON body with a password.", 400);
  }

  if (typeof password !== "string" || !(await passwordMatches(password))) {
    recordFailedLogin(client);
    return jsonError("That password isn't right.", 401);
  }

  clearFailedLogins(client);
  const response = NextResponse.json({ ok: true });
  response.cookies.set(SESSION_COOKIE, await createSession(), {
    httpOnly: true,
    sameSite: "lax",
    secure: usesHttps(request),
    path: "/",
    maxAge: SESSION_SECONDS,
  });
  return response;
}
