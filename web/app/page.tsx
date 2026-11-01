import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { Dashboard } from "@/components/Dashboard";
import { CoreError, getDashboard } from "@/lib/core";
import { SESSION_COOKIE, verifySession } from "@/lib/session";
import type { Dashboard as DashboardData } from "@/lib/types";

export const dynamic = "force-dynamic";

export default async function HomePage() {
  const store = await cookies();
  if (!(await verifySession(store.get(SESSION_COOKIE)?.value))) redirect("/login");

  let initial: DashboardData | null = null;
  try {
    initial = await getDashboard();
  } catch (error) {
    if (!(error instanceof CoreError)) throw error;
  }
  return <Dashboard initial={initial} />;
}
