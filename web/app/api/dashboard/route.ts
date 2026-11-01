import { NextResponse, type NextRequest } from "next/server";
import { getDashboard } from "@/lib/core";
import { coreErrorResponse, requireSession } from "@/lib/guard";

export const dynamic = "force-dynamic";

export async function GET(request: NextRequest) {
  const denied = await requireSession(request);
  if (denied) return denied;
  try {
    return NextResponse.json(await getDashboard(), { headers: { "Cache-Control": "no-store" } });
  } catch (error) {
    return coreErrorResponse(error);
  }
}
