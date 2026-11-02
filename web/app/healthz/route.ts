export const dynamic = "force-dynamic";

/** For reverse proxy health checks. Reveals nothing, so it needs no login. */
export function GET() {
  return Response.json({ status: "ok" });
}
