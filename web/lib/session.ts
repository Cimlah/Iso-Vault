export const SESSION_COOKIE = "isovault_session";
export const SESSION_SECONDS = 12 * 60 * 60;

const encoder = new TextEncoder();

function secret(): string {
  const value = process.env.SESSION_SECRET;
  if (!value) throw new Error("SESSION_SECRET isn't set.");
  return value;
}

async function hmac(message: string, key: string = secret()): Promise<Uint8Array> {
  const cryptoKey = await crypto.subtle.importKey(
    "raw",
    encoder.encode(key),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  return new Uint8Array(await crypto.subtle.sign("HMAC", cryptoKey, encoder.encode(message)));
}

function toHex(bytes: Uint8Array): string {
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function constantTimeEqual(a: Uint8Array, b: Uint8Array): boolean {
  if (a.length !== b.length) return false;
  let difference = 0;
  for (let index = 0; index < a.length; index += 1) difference |= a[index] ^ b[index];
  return difference === 0;
}

/** Hashing both sides first means the comparison can't reveal the password's length. */
export async function passwordMatches(candidate: string): Promise<boolean> {
  const expected = process.env.ADMIN_PASSWORD;
  if (!expected) return false;
  const [a, b] = await Promise.all([hmac(`pw:${candidate}`), hmac(`pw:${expected}`)]);
  return constantTimeEqual(a, b);
}

export async function createSession(now: number = Date.now()): Promise<string> {
  const expires = Math.floor(now / 1000) + SESSION_SECONDS;
  const nonce = toHex(crypto.getRandomValues(new Uint8Array(16)));
  const body = `${expires}.${nonce}`;
  return `${body}.${toHex(await hmac(`session:${body}`))}`;
}

export async function verifySession(token: string | undefined, now: number = Date.now()): Promise<boolean> {
  if (!token) return false;
  const parts = token.split(".");
  if (parts.length !== 3) return false;
  const [expires, nonce, signature] = parts;
  if (!/^\d+$/.test(expires) || Number(expires) * 1000 <= now) return false;
  const expected = toHex(await hmac(`session:${expires}.${nonce}`));
  return constantTimeEqual(encoder.encode(signature), encoder.encode(expected));
}
