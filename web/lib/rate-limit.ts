const WINDOW_MS = 60_000;
const MAX_PER_CLIENT = 5;
// The client address comes from a header a client could fake, so this is the real backstop.
const MAX_OVERALL = 30;
const MAX_TRACKED = 1024;

interface Bucket {
  count: number;
  resets: number;
}

const clients = new Map<string, Bucket>();
let overall: Bucket = { count: 0, resets: 0 };

function isFull(bucket: Bucket | undefined, limit: number, now: number): boolean {
  return bucket !== undefined && now < bucket.resets && bucket.count >= limit;
}

function bump(bucket: Bucket | undefined, now: number): Bucket {
  if (!bucket || now >= bucket.resets) return { count: 1, resets: now + WINDOW_MS };
  bucket.count += 1;
  return bucket;
}

export function loginBlocked(client: string, now: number = Date.now()): boolean {
  return isFull(clients.get(client), MAX_PER_CLIENT, now) || isFull(overall, MAX_OVERALL, now);
}

export function recordFailedLogin(client: string, now: number = Date.now()): void {
  if (clients.size >= MAX_TRACKED && !clients.has(client)) {
    for (const [key, bucket] of clients) {
      if (now >= bucket.resets) clients.delete(key);
    }
    if (clients.size >= MAX_TRACKED) clients.clear();
  }
  clients.set(client, bump(clients.get(client), now));
  overall = bump(overall, now);
}

export function clearFailedLogins(client: string): void {
  clients.delete(client);
}

export function resetRateLimits(): void {
  clients.clear();
  overall = { count: 0, resets: 0 };
}
