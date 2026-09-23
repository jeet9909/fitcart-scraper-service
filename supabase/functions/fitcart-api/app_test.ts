import { assert, assertEquals } from "jsr:@std/assert@1";

import { createHandler, routePath } from "./app.ts";
import type { Settings } from "./config.ts";
import { parseProduct, ScrapeProviderError, type ScrapeResponse, stripSecurityWrapper } from "./scraper.ts";
import { isGlobalAddress } from "./security.ts";
import { findImage, type TryOnService, validateImage } from "./tryon.ts";

const SETTINGS: Settings = {
  brightdataApiToken: "test",
  geminiApiKey: "test",
  geminiImageModel: "gemini-test",
  supabaseUrl: "https://example.supabase.co",
  supabaseServiceRoleKey: "service",
  supabaseStorageBucket: "fitcart-tryons",
  anonymousTokenSecret: "x".repeat(40),
  anonymousTokenDays: 30,
  gallerySignedUrlSeconds: 3600,
  maxImageBytes: 10_000_000,
  scrapeTimeoutSeconds: 60,
  // IP-literal hosts skip DNS so tests never touch the network.
  allowedProductHosts: [],
  allowedOrigins: ["https://jeet9909.github.io"],
};

const fakeScraper = {
  scrape: (url: string): Promise<ScrapeResponse> =>
    Promise.resolve({
      data: parseProduct("# Green Shirt\n₹999\n![x](https://93.184.216.34/shirt.jpg)", url),
      scraped_at: "2026-09-22T00:00:00.000Z",
      provider: "brightdata_mcp",
    }),
};

const fakeTryOn = {
  ensureConfigured() {},
  listGallery: () => Promise.resolve([]),
} as unknown as TryOnService;

const handler = createHandler({ settings: SETTINGS, scraper: fakeScraper, tryon: fakeTryOn });
const base = "http://localhost/fitcart-api";

Deno.test("routes strip the Supabase function prefix", () => {
  assertEquals(routePath("/fitcart-api/v1/products/scrape"), "/v1/products/scrape");
  assertEquals(routePath("/functions/v1/fitcart-api/health"), "/health");
  assertEquals(routePath("/fitcart-api"), "/");
  assertEquals(routePath("/v1/gallery"), "/v1/gallery");
});

Deno.test("health responds with CORS for GitHub Pages", async () => {
  const response = await handler(new Request(`${base}/health`, { headers: { Origin: "https://jeet9909.github.io" } }));
  assertEquals(await response.json(), { status: "ok" });
  assertEquals(response.headers.get("access-control-allow-origin"), "https://jeet9909.github.io");
});

Deno.test("unknown origins get no CORS grant", async () => {
  const response = await handler(new Request(`${base}/health`, { headers: { Origin: "https://evil.example" } }));
  assertEquals(response.headers.get("access-control-allow-origin"), null);
});

Deno.test("preflight is answered", async () => {
  const response = await handler(new Request(`${base}/v1/try-ons`, { method: "OPTIONS", headers: { Origin: "https://jeet9909.github.io" } }));
  assertEquals(response.status, 204);
});

Deno.test("scrape returns normalized product", async () => {
  const response = await handler(new Request(`${base}/v1/products/scrape`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url: "https://93.184.216.34/product/1", country: "IN" }),
  }));
  assertEquals(response.status, 200);
  const body = await response.json();
  assertEquals(body.data.title, "Green Shirt");
  assertEquals(body.data.price, { amount: 999, currency: "INR" });
  assertEquals(body.provider, "brightdata_mcp");
});

Deno.test("private URLs are rejected", async () => {
  for (const url of ["http://127.0.0.1/admin", "http://localhost/x", "http://10.0.0.5/", "http://[::1]/"]) {
    const response = await handler(new Request(`${base}/v1/products/scrape`, {
      method: "POST",
      body: JSON.stringify({ url }),
    }));
    assertEquals(response.status, 400, url);
  }
});

Deno.test("provider failures map to 502 and bad share links to 400", async () => {
  for (const [code, status] of [["provider_failed", 502], ["invalid_share_link", 400]] as const) {
    const failing = createHandler({
      settings: SETTINGS,
      scraper: { scrape: () => Promise.reject(new ScrapeProviderError("nope", code)) },
      tryon: fakeTryOn,
    });
    const response = await failing(new Request(`${base}/v1/products/scrape`, {
      method: "POST",
      body: JSON.stringify({ url: "https://93.184.216.34/p" }),
    }));
    assertEquals(response.status, status);
    assertEquals((await response.json()).detail.code, code);
  }
});

Deno.test("anonymous session token unlocks the gallery", async () => {
  const session = await (await handler(new Request(`${base}/v1/sessions/anonymous`, { method: "POST" }))).json();
  assertEquals(session.token_type, "bearer");
  const ok = await handler(new Request(`${base}/v1/gallery`, { headers: { Authorization: `Bearer ${session.access_token}` } }));
  assertEquals(ok.status, 200);
  assertEquals(await ok.json(), { items: [] });
  const doubled = await handler(new Request(`${base}/v1/gallery`, { headers: { Authorization: `Bearer Bearer ${session.access_token}` } }));
  assertEquals(doubled.status, 200);
  await doubled.body?.cancel();
});

Deno.test("gallery requires a valid bearer token", async () => {
  const missing = await handler(new Request(`${base}/v1/gallery`));
  assertEquals(missing.status, 401);
  const forged = await handler(new Request(`${base}/v1/gallery`, { headers: { Authorization: "Bearer a.b.c" } }));
  assertEquals(forged.status, 401);
});

Deno.test("try-on rejects non-image uploads", async () => {
  const session = await (await handler(new Request(`${base}/v1/sessions/anonymous`, { method: "POST" }))).json();
  const form = new FormData();
  form.append("person_image", new Blob(["not an image"], { type: "image/jpeg" }), "person.jpg");
  form.append("product_image", new Blob(["nope"], { type: "image/png" }), "product.png");
  const response = await handler(new Request(`${base}/v1/try-ons`, {
    method: "POST",
    headers: { Authorization: `Bearer ${session.access_token}` },
    body: form,
  }));
  assertEquals(response.status, 400);
  await response.body?.cancel();
});

Deno.test("image sniffing accepts JPEG, PNG and WebP only", () => {
  assertEquals(validateImage(new Uint8Array([0xff, 0xd8, 0xff, 0xe0]), 100).mime, "image/jpeg");
  assertEquals(validateImage(new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), 100).ext, "png");
  assertEquals(validateImage(new TextEncoder().encode("RIFF\0\0\0\0WEBPVP8 "), 100).ext, "webp");
});

Deno.test("parser extracts prices, rating and images", () => {
  const product = parseProduct(
    "# Linen Shirt\nDeal ₹1,299 was ₹2,599\n4.3 out of 5\n1,024 ratings\nIn stock\n![a](https://img.example/a.jpg)\n![b](https://img.example/a.jpg)",
    "https://www.amazon.in/dp/X",
  );
  assertEquals(product.store, "amazon.in");
  assertEquals(product.price.amount, 1299);
  assertEquals(product.original_price?.amount, 2599);
  assertEquals(product.discount_percent, 50.02);
  assertEquals(product.rating, 4.3);
  assertEquals(product.review_count, 1024);
  assertEquals(product.availability, "in_stock");
  assertEquals(product.image_urls, ["https://img.example/a.jpg"]);
});

Deno.test("security wrapper is removed", () => {
  assertEquals(stripSecurityWrapper("=====UNTRUSTED_abc_BEGIN=====\n# Title\n=====UNTRUSTED_abc_END====="), "# Title");
});

Deno.test("address classification", () => {
  assert(isGlobalAddress("93.184.216.34"));
  assert(isGlobalAddress("2606:2800:220:1::1"));
  for (const address of ["127.0.0.1", "10.1.2.3", "172.20.0.1", "192.168.1.1", "169.254.169.254", "100.64.0.1", "::1", "fd00::1", "fe80::1", "::ffff:127.0.0.1"]) {
    assert(!isGlobalAddress(address), address);
  }
});

Deno.test("Gemini image is found in nested output", () => {
  assertEquals(findImage({ outputs: [{ content: [{ mime_type: "image/png", data: "AAA" }] }] })?.data, "AAA");
  assertEquals(findImage({ outputs: [{ text: "no image" }] }), null);
});
