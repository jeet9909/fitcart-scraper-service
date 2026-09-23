import type { Settings } from "./config.ts";
import { validatePublicUrl } from "./security.ts";

export const ALLOWED_IMAGE_TYPES: Record<string, string> = { "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp" };

export interface ImageData {
  data: Uint8Array;
  mime: string;
  ext: string;
}

export interface GalleryItem {
  id: string;
  anonymous_user_id: string;
  category: string;
  product_source: string;
  product_url: string | null;
  person_image_url: string;
  product_image_url: string;
  result_image_url: string;
  model: string;
  created_at: string;
}

interface GalleryRow {
  id: string;
  anonymous_user_id: string;
  category: string;
  product_source: string;
  product_url: string | null;
  person_path: string;
  product_path: string;
  result_path: string;
  model: string;
  created_at: string;
}

export class TryOnError extends Error {
  constructor(message: string, readonly status = 502) {
    super(message);
  }
}

function sniffImageType(data: Uint8Array): string | null {
  if (data.length >= 3 && data[0] === 0xff && data[1] === 0xd8 && data[2] === 0xff) return "image/jpeg";
  if (data.length >= 8 && [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a].every((byte, index) => data[index] === byte)) {
    return "image/png";
  }
  if (
    data.length >= 12 &&
    String.fromCharCode(...data.subarray(0, 4)) === "RIFF" &&
    String.fromCharCode(...data.subarray(8, 12)) === "WEBP"
  ) return "image/webp";
  return null;
}

export function validateImage(data: Uint8Array, maxBytes: number): ImageData {
  if (!data.length || data.length > maxBytes) {
    throw new TryOnError(`Image must be between 1 byte and ${Math.floor(maxBytes / 1_000_000)} MB`, 400);
  }
  const mime = sniffImageType(data);
  if (!mime) throw new TryOnError("The uploaded file is not a valid JPEG, PNG, or WebP image", 400);
  return { data, mime, ext: ALLOWED_IMAGE_TYPES[mime] };
}

function toBase64(bytes: Uint8Array): string {
  let binary = "";
  const chunk = 0x8000;
  for (let index = 0; index < bytes.length; index += chunk) {
    binary += String.fromCharCode(...bytes.subarray(index, index + chunk));
  }
  return btoa(binary);
}

function fromBase64(value: string): Uint8Array {
  return Uint8Array.from(atob(value), (char) => char.charCodeAt(0));
}

type Json = Record<string, unknown> | unknown[] | string | number | boolean | null | undefined;

export function findImage(value: Json): { data: string; mime_type?: string } | null {
  if (Array.isArray(value)) {
    for (const item of value) {
      const found = findImage(item as Json);
      if (found) return found;
    }
  } else if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    if (typeof record.data === "string" && String(record.mime_type ?? "").startsWith("image/")) {
      return record as { data: string; mime_type?: string };
    }
    for (const key of ["output_image", "outputs", "output", "content", "steps"]) {
      if (key in record) {
        const found = findImage(record[key] as Json);
        if (found) return found;
      }
    }
  }
  return null;
}

export class TryOnService {
  constructor(private readonly settings: Settings) {}

  private get headers(): Record<string, string> {
    const key = this.settings.supabaseServiceRoleKey;
    // New-style secret keys (sb_secret_...) are not JWTs and go in apikey only.
    return key.startsWith("sb_") ? { apikey: key } : { apikey: key, Authorization: `Bearer ${key}` };
  }

  ensureConfigured(): void {
    const missing: string[] = [];
    if (!this.settings.geminiApiKey) missing.push("GEMINI_API_KEY");
    if (!this.settings.supabaseUrl) missing.push("SUPABASE_URL");
    if (!this.settings.supabaseServiceRoleKey) missing.push("SUPABASE_SERVICE_ROLE_KEY");
    if (!this.settings.anonymousTokenSecret) missing.push("ANONYMOUS_TOKEN_SECRET");
    if (missing.length) throw new TryOnError(`Try-on service is not configured: ${missing.join(", ")}`, 503);
  }

  async fetchImage(url: string): Promise<ImageData> {
    let response: Response;
    try {
      response = await fetch(url, { headers: { "User-Agent": "FitCart/1.0" }, redirect: "follow", signal: AbortSignal.timeout(30_000) });
    } catch {
      throw new TryOnError("Could not download the product image", 502);
    }
    if (!response.ok) {
      await response.body?.cancel();
      throw new TryOnError("Could not download the product image", 502);
    }
    await validatePublicUrl(response.url || url);
    return validateImage(new Uint8Array(await response.arrayBuffer()), this.settings.maxImageBytes);
  }

  async generate(person: ImageData, product: ImageData, category: string): Promise<ImageData> {
    const prompt = "Create a photorealistic virtual try-on using the first image as the person identity and body reference " +
      "and the second image as the exact product reference. Put the product naturally on the person. " +
      "Preserve the person's face, identity, skin tone, body proportions, pose, background, camera angle, and lighting. " +
      `The product category is ${category}. Preserve its color, texture, print, logo, shape, and design. ` +
      "Do not alter unrelated clothing or add accessories. Return one full-body front-view image with no text or collage.";
    const payload = {
      model: this.settings.geminiImageModel,
      input: [
        { type: "text", text: prompt },
        { type: "image", mime_type: person.mime, data: toBase64(person.data) },
        { type: "image", mime_type: product.mime, data: toBase64(product.data) },
      ],
      response_format: { type: "image", mime_type: "image/png", aspect_ratio: "3:4", image_size: "1K" },
    };
    const response = await fetch("https://generativelanguage.googleapis.com/v1beta/interactions", {
      method: "POST",
      headers: { "x-goog-api-key": this.settings.geminiApiKey, "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(140_000),
    });
    if (response.status >= 400) {
      await response.body?.cancel();
      throw new TryOnError(`Gemini image generation failed (${response.status})`);
    }
    const image = findImage(await response.json());
    if (!image) throw new TryOnError("Gemini returned no generated image");
    let data: Uint8Array;
    try {
      data = fromBase64(image.data);
    } catch {
      throw new TryOnError("Gemini returned an invalid image");
    }
    return validateImage(data, 20_000_000);
  }

  private objectUrl(kind: "object" | "object/sign", path: string): string {
    const encoded = path.split("/").map(encodeURIComponent).join("/");
    return `${this.settings.supabaseUrl}/storage/v1/${kind}/${this.settings.supabaseStorageBucket}/${encoded}`;
  }

  private async upload(path: string, image: ImageData): Promise<void> {
    const response = await fetch(this.objectUrl("object", path), {
      method: "POST",
      headers: { ...this.headers, "Content-Type": image.mime, "x-upsert": "false" },
      body: image.data.slice().buffer as ArrayBuffer,
    });
    await response.body?.cancel();
    if (response.status >= 400) throw new TryOnError("Could not save image to the private gallery");
  }

  private async signedUrl(path: string): Promise<string> {
    const response = await fetch(this.objectUrl("object/sign", path), {
      method: "POST",
      headers: { ...this.headers, "Content-Type": "application/json" },
      body: JSON.stringify({ expiresIn: this.settings.gallerySignedUrlSeconds }),
    });
    if (response.status >= 400) {
      await response.body?.cancel();
      throw new TryOnError("Could not create a private gallery URL");
    }
    const body = await response.json();
    const signed: string | undefined = body.signedURL ?? body.signedUrl;
    if (!signed) throw new TryOnError("Supabase returned no signed URL");
    return signed.startsWith("http") ? signed : `${this.settings.supabaseUrl}/storage/v1${signed}`;
  }

  async save(
    userId: string,
    person: ImageData,
    product: ImageData,
    result: ImageData,
    category: string,
    productSource: string,
    productUrl: string | null,
  ): Promise<GalleryItem> {
    const itemId = crypto.randomUUID();
    const prefix = `${userId}/${itemId}`;
    const paths = {
      person: `${prefix}/person.${person.ext}`,
      product: `${prefix}/product.${product.ext}`,
      result: `${prefix}/result.${result.ext}`,
    };
    await Promise.all([
      this.upload(paths.person, person),
      this.upload(paths.product, product),
      this.upload(paths.result, result),
    ]);
    const row = {
      id: itemId,
      anonymous_user_id: userId,
      category,
      product_source: productSource,
      product_url: productUrl,
      person_path: paths.person,
      product_path: paths.product,
      result_path: paths.result,
      model: this.settings.geminiImageModel,
    };
    const response = await fetch(`${this.settings.supabaseUrl}/rest/v1/try_on_gallery`, {
      method: "POST",
      headers: { ...this.headers, "Content-Type": "application/json", Prefer: "return=representation" },
      body: JSON.stringify(row),
    });
    if (response.status >= 400) {
      await response.body?.cancel();
      throw new TryOnError("Could not save the gallery record");
    }
    const [created] = await response.json();
    return this.toItem(created);
  }

  async listGallery(userId: string): Promise<GalleryItem[]> {
    const params = new URLSearchParams({ anonymous_user_id: `eq.${userId}`, select: "*", order: "created_at.desc" });
    const response = await fetch(`${this.settings.supabaseUrl}/rest/v1/try_on_gallery?${params}`, { headers: this.headers });
    if (response.status >= 400) {
      await response.body?.cancel();
      throw new TryOnError("Could not load the gallery");
    }
    const rows: GalleryRow[] = await response.json();
    return Promise.all(rows.map((row) => this.toItem(row)));
  }

  private async toItem(row: GalleryRow): Promise<GalleryItem> {
    const [person, product, result] = await Promise.all([
      this.signedUrl(row.person_path),
      this.signedUrl(row.product_path),
      this.signedUrl(row.result_path),
    ]);
    return {
      id: row.id,
      anonymous_user_id: row.anonymous_user_id,
      category: row.category,
      product_source: row.product_source,
      product_url: row.product_url ?? null,
      person_image_url: person,
      product_image_url: product,
      result_image_url: result,
      model: row.model,
      created_at: new Date(row.created_at).toISOString(),
    };
  }
}
