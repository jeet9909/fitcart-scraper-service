import { Client } from "npm:@modelcontextprotocol/sdk@1/client/index.js";
import { StreamableHTTPClientTransport } from "npm:@modelcontextprotocol/sdk@1/client/streamableHttp.js";

import type { Settings } from "./config.ts";

const SHARE_HOST_DESTINATIONS: Record<string, string[]> = {
  "amzn.in": ["amazon.in"],
  "fkrt.it": ["flipkart.com"],
};

export class ScrapeProviderError extends Error {
  constructor(message: string, readonly code = "provider_failed") {
    super(message);
  }
}

export interface Money {
  amount: number | null;
  currency: string | null;
}

export interface ProductData {
  source_url: string;
  store: string | null;
  external_id: string | null;
  title: string;
  brand: string | null;
  description: string | null;
  category: string | null;
  price: Money;
  original_price: Money | null;
  discount_percent: number | null;
  availability: "in_stock" | "out_of_stock" | "unknown";
  rating: number | null;
  review_count: number | null;
  image_urls: string[];
  colors: string[];
  sizes: string[];
  material: string | null;
  seller: string | null;
}

export interface ScrapeResponse {
  data: ProductData;
  scraped_at: string;
  provider: "brightdata_mcp";
}

export interface Scraper {
  scrape(url: string, country: string): Promise<ScrapeResponse>;
}

function amount(value: string): number | null {
  const parsed = Number(value.replaceAll(",", "").trim());
  return Number.isFinite(parsed) ? parsed : null;
}

export function stripSecurityWrapper(text: string): string {
  const match = text.match(/=====UNTRUSTED_([A-Za-z0-9]+)_BEGIN=====\s*([\s\S]*?)\s*=====UNTRUSTED_\1_END=====/);
  return match ? match[2].trim() : text.trim();
}

function isAllowedDestination(host: string, allowedRoots: string[]): boolean {
  return allowedRoots.some((root) => host === root || host.endsWith(`.${root}`));
}

export async function resolveShareUrl(url: string, fetcher: typeof fetch = fetch): Promise<string> {
  const host = new URL(url).hostname.toLowerCase();
  const allowedRoots = SHARE_HOST_DESTINATIONS[host];
  if (!allowedRoots) return url;

  const headers = { "User-Agent": "Mozilla/5.0 (compatible; FitCartScraper/1.0)" };
  let lastError: unknown;
  for (const method of ["HEAD", "GET"]) {
    try {
      const response = await fetcher(url, { method, headers, redirect: "follow", signal: AbortSignal.timeout(12_000) });
      await response.body?.cancel();
      if (response.status >= 400) throw new Error(`HTTP ${response.status}`);
      const resolved = response.url || url;
      if (!isAllowedDestination(new URL(resolved).hostname.toLowerCase(), allowedRoots)) {
        throw new ScrapeProviderError("Product share link redirected to an unexpected domain");
      }
      return resolved;
    } catch (error) {
      if (error instanceof ScrapeProviderError) throw error;
      lastError = error;
    }
  }
  console.warn(`Could not resolve product share URL host=${host} error=${lastError}`);
  throw new ScrapeProviderError("The product share URL is invalid, expired, or unavailable", "invalid_share_link");
}

export function parseProduct(markdown: string, url: string): ProductData {
  const lines = markdown.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const headings = lines.filter((line) => /^#{1,3}\s+/.test(line)).map((line) => line.replace(/^#+\s*/, "").trim());
  const hostname = new URL(url).hostname;
  const title = headings[0] ?? (lines[0]?.slice(0, 300) || hostname || "Product");

  const prices = [...markdown.matchAll(/(?:₹|INR\s*)\s*([0-9][0-9,]*(?:\.\d{1,2})?)/gi)]
    .map((match) => amount(match[1]))
    .filter((value): value is number => value !== null);
  const price = prices[0] ?? null;
  const original = prices.slice(1).find((candidate) => price !== null && candidate > price) ?? null;
  const discount = price !== null && original ? Math.round(((original - price) / original) * 10000) / 100 : null;

  const images: string[] = [];
  for (const match of markdown.matchAll(/!\[[^\]]*\]\((https?:\/\/[^\s)]+)/g)) {
    if (!images.includes(match[1])) images.push(match[1]);
  }

  const ratingMatch = markdown.match(/\b([0-4](?:\.\d+)?|5(?:\.0+)?)\s*(?:out of 5|\/\s*5|stars?|★)/i);
  const reviewsMatch = markdown.match(/([0-9][0-9,]*)\s+(?:ratings?|reviews?)/i);

  const lowered = markdown.toLowerCase();
  const availability = ["out of stock", "currently unavailable", "sold out"].some((term) => lowered.includes(term))
    ? "out_of_stock"
    : ["in stock", "add to cart", "buy now"].some((term) => lowered.includes(term))
    ? "in_stock"
    : "unknown";
  const descriptionLines = lines.filter((line) => !line.startsWith("#") && !line.startsWith("![") && !line.startsWith("["));
  const description = descriptionLines.slice(0, 8).join(" ").slice(0, 2000) || null;

  return {
    source_url: url,
    store: hostname.replace(/^www\./, ""),
    external_id: null,
    title: title.slice(0, 500),
    brand: null,
    description,
    category: null,
    price: { amount: price, currency: "INR" },
    original_price: original !== null ? { amount: original, currency: "INR" } : null,
    discount_percent: discount,
    availability,
    rating: ratingMatch ? Number(ratingMatch[1]) : null,
    review_count: reviewsMatch ? Number(reviewsMatch[1].replaceAll(",", "")) : null,
    image_urls: images.slice(0, 30),
    colors: [],
    sizes: [],
    material: null,
    seller: null,
  };
}

function withTimeout<T>(promise: Promise<T>, seconds: number): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new ScrapeProviderError("Product scraping timed out")), seconds * 1000);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

export class BrightDataScraper implements Scraper {
  constructor(private readonly settings: Settings) {}

  private async fetchPage(url: string): Promise<string> {
    const token = this.settings.brightdataApiToken;
    const transport = new StreamableHTTPClientTransport(new URL(`https://mcp.brightdata.com/mcp?token=${encodeURIComponent(token)}`));
    const client = new Client({ name: "fitcart-api", version: "1.0.0" });
    try {
      await client.connect(transport);
      const result = await client.callTool({ name: "scrape_as_markdown", arguments: { url } });
      const content = (result.content ?? []) as Array<{ type: string; text?: string }>;
      const text = content.filter((block) => block.type === "text").map((block) => block.text ?? "").join("\n");
      if (result.isError || !text.trim()) {
        throw new ScrapeProviderError(text.trim() || "Bright Data returned no page content");
      }
      return text;
    } finally {
      await client.close().catch(() => {});
    }
  }

  async scrape(url: string, _country: string): Promise<ScrapeResponse> {
    if (!this.settings.brightdataApiToken) {
      throw new ScrapeProviderError("Product scraping is not configured: BRIGHTDATA_API_TOKEN");
    }
    let product: ProductData;
    try {
      const resolvedUrl = await resolveShareUrl(url);
      let content = await withTimeout(this.fetchPage(resolvedUrl), this.settings.scrapeTimeoutSeconds);
      content = stripSecurityWrapper(content);
      if (content.toLowerCase().includes("page not found") && content.length < 500) {
        throw new ScrapeProviderError("The product page was not found");
      }
      product = parseProduct(content, url);
    } catch (error) {
      if (error instanceof ScrapeProviderError) throw error;
      const token = this.settings.brightdataApiToken;
      const safeMessage = String((error as Error)?.message ?? error).replaceAll(token, "[REDACTED]");
      console.error(`Product scraping failed host=${new URL(url).hostname} error=${safeMessage.slice(0, 2000)}`);
      throw new ScrapeProviderError(`Product scraping failed: ${safeMessage.slice(0, 500)}`);
    }
    return { data: product, scraped_at: new Date().toISOString(), provider: "brightdata_mcp" };
  }
}
