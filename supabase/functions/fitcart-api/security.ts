export class UnsafeUrlError extends Error {}

function hostIsAllowed(hostname: string, allowedHosts: string[]): boolean {
  return !allowedHosts.length ||
    allowedHosts.some((allowed) => hostname === allowed || hostname.endsWith(`.${allowed}`));
}

function ipv4IsGlobal(address: string): boolean {
  const parts = address.split(".").map(Number);
  if (parts.length !== 4 || parts.some((part) => !Number.isInteger(part) || part < 0 || part > 255)) return false;
  const [a, b] = parts;
  if (a === 0 || a === 10 || a === 127 || a >= 224) return false;
  if (a === 100 && b >= 64 && b <= 127) return false; // carrier-grade NAT
  if (a === 169 && b === 254) return false;
  if (a === 172 && b >= 16 && b <= 31) return false;
  if (a === 192 && (b === 168 || (b === 0 && (parts[2] === 0 || parts[2] === 2)))) return false;
  if (a === 198 && (b === 18 || b === 19 || (b === 51 && parts[2] === 100))) return false;
  if (a === 203 && b === 0 && parts[2] === 113) return false;
  return true;
}

function ipv6IsGlobal(address: string): boolean {
  const value = address.toLowerCase().replace(/^\[|\]$/g, "");
  if (value === "::" || value === "::1") return false;
  const mapped = value.match(/^::ffff:(\d+\.\d+\.\d+\.\d+)$/);
  if (mapped) return ipv4IsGlobal(mapped[1]);
  // Only 2000::/3 is globally routable unicast.
  const first = parseInt(value.split(":")[0] || "0", 16);
  if (!(first >= 0x2000 && first <= 0x3fff)) return false;
  return !value.startsWith("2001:db8") && !value.startsWith("2001:0db8");
}

export function isGlobalAddress(address: string): boolean {
  return address.includes(":") ? ipv6IsGlobal(address) : ipv4IsGlobal(address);
}

async function resolveAddresses(hostname: string): Promise<string[] | null> {
  const resolve = (Deno as unknown as { resolveDns?: (host: string, type: string) => Promise<string[]> }).resolveDns;
  if (!resolve) return null;
  const results = await Promise.allSettled([resolve(hostname, "A"), resolve(hostname, "AAAA")]);
  const addresses = results.flatMap((result) => result.status === "fulfilled" ? result.value : []);
  if (addresses.length) return addresses;
  // Distinguish "no DNS support in this runtime" from "host does not exist".
  const reasons = results.map((result) => result.status === "rejected" ? String(result.reason) : "");
  if (reasons.some((reason) => /not ?supported|permission|NotCapable/i.test(reason))) return null;
  throw new UnsafeUrlError("The product URL host could not be resolved");
}

export async function validatePublicUrl(url: string, allowedHosts: string[] = []): Promise<string> {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    throw new UnsafeUrlError("Only absolute HTTP or HTTPS product URLs are accepted");
  }
  if (!["http:", "https:"].includes(parsed.protocol) || !parsed.hostname || parsed.username || parsed.password) {
    throw new UnsafeUrlError("Only absolute HTTP or HTTPS product URLs are accepted");
  }

  const hostname = parsed.hostname.replace(/\.$/, "").toLowerCase();
  if (hostname === "localhost" || hostname.endsWith(".localhost") || !hostIsAllowed(hostname, allowedHosts)) {
    throw new UnsafeUrlError("The product URL host is not allowed");
  }

  const literal = /^\d+\.\d+\.\d+\.\d+$/.test(hostname) || hostname.startsWith("[");
  const addresses = literal ? [hostname] : await resolveAddresses(hostname);
  for (const address of addresses ?? []) {
    if (!isGlobalAddress(address)) {
      throw new UnsafeUrlError("Private, local, and reserved network targets are not allowed");
    }
  }
  return url;
}
