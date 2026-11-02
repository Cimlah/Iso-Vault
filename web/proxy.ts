import { NextResponse, type NextRequest } from "next/server";
import { SESSION_COOKIE, verifySession } from "@/lib/session";

/** A first line of defence. Route handlers check the session again themselves. */
export async function proxy(request: NextRequest) {
  const { pathname } = request.nextUrl;
  if (pathname === "/login" || pathname === "/api/login") return NextResponse.next();

  if (await verifySession(request.cookies.get(SESSION_COOKIE)?.value)) return NextResponse.next();

  if (pathname.startsWith("/api/")) {
    return NextResponse.json({ error: "Please log in." }, { status: 401 });
  }
  return NextResponse.redirect(new URL("/login", publicBase(request)));
}

/**
 * Behind a reverse proxy, the URL Next.js sees isn't the address the browser used. This follows the
 * request rather than UI_PUBLIC_URL, so the direct port still works if the proxy isn't set up yet.
 */
function publicBase(request: NextRequest): string {
  const host = request.headers.get("x-forwarded-host") ?? request.headers.get("host");
  const protocol = request.headers.get("x-forwarded-proto") ?? "http";
  return host ? `${protocol}://${host}` : request.url;
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|healthz|robots.txt|icon.svg).*)"],
};
