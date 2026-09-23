import type { Settings } from "./config.ts";
import { HttpError } from "./http.ts";

const encoder = new TextEncoder();

function base64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function fromBase64Url(value: string): ArrayBuffer {
  const padded = value.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(value.length / 4) * 4, "=");
  return Uint8Array.from(atob(padded), (char) => char.charCodeAt(0)).buffer as ArrayBuffer;
}

function hmacKey(secret: string): Promise<CryptoKey> {
  return crypto.subtle.importKey("raw", encoder.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign", "verify"]);
}

function requireSecret(settings: Settings): string {
  const secret = settings.anonymousTokenSecret;
  if (secret.length < 32) {
    throw new HttpError(503, "Anonymous sessions are not configured");
  }
  return secret;
}

export async function createAnonymousSession(settings: Settings) {
  const secret = requireSecret(settings);
  const userId = crypto.randomUUID();
  const expiresAt = new Date(Date.now() + settings.anonymousTokenDays * 86_400_000);
  const header = base64Url(encoder.encode(JSON.stringify({ alg: "HS256", typ: "JWT" })));
  const payload = base64Url(encoder.encode(JSON.stringify({
    sub: userId,
    exp: Math.floor(expiresAt.getTime() / 1000),
    type: "anonymous",
  })));
  const signature = await crypto.subtle.sign("HMAC", await hmacKey(secret), encoder.encode(`${header}.${payload}`));
  return {
    anonymous_user_id: userId,
    access_token: `${header}.${payload}.${base64Url(new Uint8Array(signature))}`,
    token_type: "bearer",
    expires_at: expiresAt.toISOString(),
  };
}

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export async function verifyAnonymousToken(token: string, settings: Settings): Promise<string> {
  const invalid = new HttpError(401, "Invalid or expired anonymous token");
  const secret = settings.anonymousTokenSecret;
  const parts = token.split(".");
  if (!secret || parts.length !== 3) throw invalid;
  try {
    const header = JSON.parse(new TextDecoder().decode(fromBase64Url(parts[0])));
    if (header.alg !== "HS256") throw invalid;
    const valid = await crypto.subtle.verify(
      "HMAC",
      await hmacKey(secret),
      fromBase64Url(parts[2]),
      encoder.encode(`${parts[0]}.${parts[1]}`),
    );
    if (!valid) throw invalid;
    const payload = JSON.parse(new TextDecoder().decode(fromBase64Url(parts[1])));
    if (payload.type !== "anonymous" || typeof payload.exp !== "number" || payload.exp * 1000 <= Date.now()) throw invalid;
    if (typeof payload.sub !== "string" || !UUID_PATTERN.test(payload.sub)) throw invalid;
    return payload.sub.toLowerCase();
  } catch {
    throw invalid;
  }
}

export function bearerToken(request: Request): string {
  const header = request.headers.get("authorization") ?? "";
  const match = header.match(/^\s*bearer\s+(.+)$/i);
  if (!match) throw new HttpError(401, "Bearer token required");
  let token = match[1].trim();
  // Accept a pasted value that already starts with "Bearer ".
  if (token.toLowerCase().startsWith("bearer ")) token = token.slice(7).trim();
  return token;
}
