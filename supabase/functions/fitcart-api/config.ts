export const OFFICIAL_PRODUCT_SHARE_HOSTS = ["amzn.in", "fkrt.it"];

const DEFAULT_ALLOWED_ORIGINS = [
  "https://jeet9909.github.io",
  "http://localhost:5173",
  "http://127.0.0.1:5173",
  "http://localhost:8000",
  "http://127.0.0.1:8000",
];

function env(name: string, fallback = ""): string {
  return (Deno.env.get(name) ?? fallback).trim();
}

function csv(value: string): string[] {
  return value.split(",").map((item) => item.trim().toLowerCase()).filter(Boolean);
}

function bounded(name: string, fallback: number, min: number, max: number): number {
  const parsed = Number(env(name));
  return Number.isFinite(parsed) && parsed >= min && parsed <= max ? parsed : fallback;
}

export interface Settings {
  brightdataApiToken: string;
  geminiApiKey: string;
  geminiImageModel: string;
  supabaseUrl: string;
  supabaseServiceRoleKey: string;
  supabaseStorageBucket: string;
  anonymousTokenSecret: string;
  anonymousTokenDays: number;
  gallerySignedUrlSeconds: number;
  maxImageBytes: number;
  scrapeTimeoutSeconds: number;
  allowedProductHosts: string[];
  allowedOrigins: string[];
}

export function loadSettings(): Settings {
  const configuredHosts = csv(env("ALLOWED_PRODUCT_HOSTS"));
  const origins = csv(env("ALLOWED_ORIGINS"));
  return {
    brightdataApiToken: env("BRIGHTDATA_API_TOKEN"),
    geminiApiKey: env("GEMINI_API_KEY"),
    geminiImageModel: env("GEMINI_IMAGE_MODEL", "gemini-3.1-flash-image"),
    // SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are injected automatically
    // into every Supabase Edge Function.
    supabaseUrl: env("SUPABASE_URL").replace(/\/+$/, ""),
    supabaseServiceRoleKey: env("SUPABASE_SERVICE_ROLE_KEY"),
    supabaseStorageBucket: env("SUPABASE_STORAGE_BUCKET", "fitcart-tryons"),
    anonymousTokenSecret: env("ANONYMOUS_TOKEN_SECRET"),
    anonymousTokenDays: bounded("ANONYMOUS_TOKEN_DAYS", 30, 1, 365),
    gallerySignedUrlSeconds: bounded("GALLERY_SIGNED_URL_SECONDS", 3600, 60, 86400),
    maxImageBytes: bounded("MAX_IMAGE_BYTES", 10_000_000, 100_000, 20_000_000),
    scrapeTimeoutSeconds: bounded("SCRAPE_TIMEOUT_SECONDS", 60, 1, 140),
    // Official store share links use separate redirect domains. Keep these
    // accepted even when a production allowlist is configured.
    allowedProductHosts: configuredHosts.length
      ? [...new Set([...configuredHosts, ...OFFICIAL_PRODUCT_SHARE_HOSTS])]
      : [],
    allowedOrigins: origins.length ? origins : DEFAULT_ALLOWED_ORIGINS,
  };
}
