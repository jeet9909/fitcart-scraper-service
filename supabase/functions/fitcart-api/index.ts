// FitCart Product and Virtual Try-On API as a Supabase Edge Function.
//
// Deployed URL: https://<project-ref>.supabase.co/functions/v1/fitcart-api
// Routes mirror the FastAPI service in app/main.py:
//   GET  /health, /ready
//   POST /v1/products/scrape
//   POST /v1/sessions/anonymous
//   POST /v1/try-ons          (Bearer anonymous token, multipart/form-data)
//   GET  /v1/gallery          (Bearer anonymous token)
import { createHandler } from "./app.ts";
import { loadSettings } from "./config.ts";
import { BrightDataScraper } from "./scraper.ts";
import { TryOnService } from "./tryon.ts";

const settings = loadSettings();

Deno.serve(createHandler({
  settings,
  scraper: new BrightDataScraper(settings),
  tryon: new TryOnService(settings),
}));
