import { NextResponse, type NextRequest } from "next/server";
import { coreRequest } from "@/lib/core";
import { coreErrorResponse, jsonError, requireWriteAccess } from "@/lib/guard";
import { MAC_PATTERN } from "@/lib/format";

interface Context {
  params: Promise<{ mac: string }>;
}

async function validMac(context: Context): Promise<string | null> {
  const { mac } = await context.params;
  const decoded = decodeURIComponent(mac);
  return MAC_PATTERN.test(decoded) ? decoded : null;
}

export async function PUT(request: NextRequest, context: Context) {
  const denied = await requireWriteAccess(request);
  if (denied) return denied;

  const mac = await validMac(context);
  if (!mac) return jsonError("That isn't a valid MAC address.", 400);

  let body: { iso_id?: unknown; mode?: unknown };
  try {
    body = await request.json();
  } catch {
    return jsonError("Send a JSON body with an iso_id.", 400);
  }
  if (typeof body.iso_id !== "string" || !body.iso_id) return jsonError("Choose an ISO.", 400);
  if (body.mode !== "once" && body.mode !== "always") return jsonError("Choose a pin type.", 400);

  try {
    const pin = await coreRequest("PUT", `/api/pins/${encodeURIComponent(mac)}`, {
      iso_id: body.iso_id,
      mode: body.mode,
    });
    return NextResponse.json(pin);
  } catch (error) {
    return coreErrorResponse(error);
  }
}

export async function DELETE(request: NextRequest, context: Context) {
  const denied = await requireWriteAccess(request);
  if (denied) return denied;

  const mac = await validMac(context);
  if (!mac) return jsonError("That isn't a valid MAC address.", 400);

  try {
    return NextResponse.json(await coreRequest("DELETE", `/api/pins/${encodeURIComponent(mac)}`));
  } catch (error) {
    return coreErrorResponse(error);
  }
}
