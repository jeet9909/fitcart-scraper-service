import { bearerToken, createAnonymousSession, verifyAnonymousToken } from "./auth.ts";
import type { Settings } from "./config.ts";
import { corsHeaders, HttpError, json } from "./http.ts";
import { ScrapeProviderError, type Scraper } from "./scraper.ts";
import { UnsafeUrlError, validatePublicUrl } from "./security.ts";
import { type ImageData, TryOnError, type TryOnService, validateImage } from "./tryon.ts";

export interface Dependencies {
  settings: Settings;
  scraper: Scraper;
  tryon: TryOnService;
}

// Supabase routes /functions/v1/fitcart-api/<path> to this function with the
// pathname "/fitcart-api/<path>". Strip that prefix so routes match the
// FastAPI service exactly.
export function routePath(pathname: string): string {
  const stripped = pathname.replace(/^.*?\/fitcart-api(?=\/|$)/, "");
  return stripped.replace(/\/+$/, "") || "/";
}

async function readJson(request: Request): Promise<Record<string, unknown>> {
  try {
    const body = await request.json();
    if (body && typeof body === "object" && !Array.isArray(body)) return body;
  } catch {
    // Fall through to validation error below.
  }
  throw new HttpError(422, "Request body must be a JSON object");
}

function country(value: unknown): string {
  const code = typeof value === "string" ? value.trim().toUpperCase() : "IN";
  if (!/^[A-Z]{2}$/.test(code)) throw new HttpError(422, "country must be a two-letter ISO code");
  return code;
}

async function scrapeProduct(request: Request, deps: Dependencies): Promise<Response> {
  const body = await readJson(request);
  if (typeof body.url !== "string" || !body.url.trim()) throw new HttpError(422, "url is required");
  const url = await validatePublicUrl(body.url.trim(), deps.settings.allowedProductHosts);
  return json(await deps.scraper.scrape(url, country(body.country ?? "IN")));
}

async function formImage(value: FormDataEntryValue | null, settings: Settings): Promise<ImageData | null> {
  if (value === null) return null;
  if (typeof value === "string") throw new TryOnError("Image fields must be file uploads", 400);
  return validateImage(new Uint8Array(await value.arrayBuffer()), settings.maxImageBytes);
}

function optionalText(form: FormData, name: string): string | null {
  const value = form.get(name);
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

async function createTryOn(request: Request, deps: Dependencies): Promise<Response> {
  const userId = await verifyAnonymousToken(bearerToken(request), deps.settings);
  deps.tryon.ensureConfigured();

  let form: FormData;
  try {
    form = await request.formData();
  } catch {
    throw new HttpError(422, "Request body must be multipart/form-data");
  }
  const productUpload = form.get("product_image");
  const productPageUrl = optionalText(form, "product_page_url");
  const productImageUrl = optionalText(form, "product_image_url");
  const sources = [productUpload, productPageUrl, productImageUrl].filter((value) => value !== null).length;
  if (sources !== 1) {
    throw new TryOnError("Provide exactly one product source: product_image, product_page_url, or product_image_url", 400);
  }
  const person = await formImage(form.get("person_image"), deps.settings);
  if (!person) throw new HttpError(422, "person_image is required");
  const category = (optionalText(form, "category") ?? "clothing").slice(0, 80);

  let product: ImageData;
  let productSource: string;
  let sourceUrl: string | null = null;
  if (productUpload !== null) {
    product = (await formImage(productUpload, deps.settings))!;
    productSource = "upload";
  } else if (productImageUrl !== null) {
    sourceUrl = await validatePublicUrl(productImageUrl);
    product = await deps.tryon.fetchImage(sourceUrl);
    productSource = "image_url";
  } else {
    sourceUrl = await validatePublicUrl(productPageUrl!, deps.settings.allowedProductHosts);
    const scraped = await deps.scraper.scrape(sourceUrl, country(optionalText(form, "country") ?? "IN"));
    if (!scraped.data.image_urls.length) {
      throw new TryOnError("The scraped product page did not provide a usable product image; upload the product image directly", 422);
    }
    product = await deps.tryon.fetchImage(await validatePublicUrl(scraped.data.image_urls[0]));
    productSource = "scraped_url";
  }

  const result = await deps.tryon.generate(person, product, category);
  return json(await deps.tryon.save(userId, person, product, result, category, productSource, sourceUrl));
}

async function route(request: Request, deps: Dependencies): Promise<Response> {
  const path = routePath(new URL(request.url).pathname);
  const key = `${request.method} ${path}`;
  switch (key) {
    case "GET /":
    case "GET /health":
      return json({ status: "ok" });
    case "GET /ready":
      if (!deps.settings.brightdataApiToken) throw new HttpError(503, "Service is not configured");
      return json({ status: "ok" });
    case "POST /v1/products/scrape":
      return scrapeProduct(request, deps);
    case "POST /v1/sessions/anonymous":
      return json(await createAnonymousSession(deps.settings));
    case "POST /v1/try-ons":
      return createTryOn(request, deps);
    case "GET /v1/gallery": {
      const userId = await verifyAnonymousToken(bearerToken(request), deps.settings);
      deps.tryon.ensureConfigured();
      return json({ items: await deps.tryon.listGallery(userId) });
    }
  }
  throw new HttpError(404, "Not Found");
}

export function createHandler(deps: Dependencies): (request: Request) => Promise<Response> {
  return async (request: Request) => {
    const cors = corsHeaders(request, deps.settings.allowedOrigins);
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: cors });
    try {
      const response = await route(request, deps);
      for (const [name, value] of Object.entries(cors)) response.headers.set(name, value);
      return response;
    } catch (error) {
      if (error instanceof HttpError) return json({ detail: error.detail }, error.status, cors);
      if (error instanceof UnsafeUrlError) return json({ detail: error.message }, 400, cors);
      if (error instanceof ScrapeProviderError) {
        const status = error.code === "invalid_share_link" ? 400 : 502;
        return json({ detail: { code: error.code, message: error.message } }, status, cors);
      }
      if (error instanceof TryOnError) return json({ detail: error.message }, error.status, cors);
      console.error("Unhandled FitCart API error", error);
      return json({ detail: "Internal server error" }, 500, cors);
    }
  };
}
