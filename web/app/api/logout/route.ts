import { NextResponse, type NextRequest } from "next/server";
import { requireWriteAccess } from "@/lib/guard";
import { SESSION_COOKIE } from "@/lib/session";

export async function POST(request: NextRequest) {
  const denied = await requireWriteAccess(request);
  if (denied) return denied;
  const response = NextResponse.json({ ok: true });
  response.cookies.delete(SESSION_COOKIE);
  return response;
}
