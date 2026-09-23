# FitCart Product and Virtual Try-On API

Standalone product scraping and Gemini virtual try-on API for FitCart. Product pages are fetched through Bright Data MCP. Try-on images are generated with Gemini and saved in a private Supabase gallery.

## What this repository contains

- `POST /v1/products/scrape` for a single product-share URL
- `POST /v1/sessions/anonymous` for a private anonymous gallery token
- `POST /v1/try-ons` with a full-body photo plus a product upload, image URL, or scraped product-page URL
- `GET /v1/gallery` for that anonymous user's private gallery
- Direct Bright Data MCP integration without OpenAI credits
- Gemini multi-reference image editing and private Supabase Storage
- Normalized price, inventory, images, variants, rating, seller, and product metadata
- Public-URL validation to block localhost/private-network targets
- Concurrency and timeout controls
- Docker deployment and automated tests
- The approved earthy FitCart storefront, served from `/` and connected directly to the API

It intentionally does not contain carts or affiliate checkout redirects.

## Configure

Copy `.env.example` to `.env` and configure Bright Data, Gemini, Supabase, and a random token secret:

```env
OPENAI_API_KEY=...
BRIGHTDATA_API_TOKEN=...
GEMINI_API_KEY=...
SUPABASE_URL=https://YOUR_PROJECT.supabase.co
SUPABASE_SERVICE_ROLE_KEY=...
ANONYMOUS_TOKEN_SECRET=...
```

Run `supabase/schema.sql` once in the Supabase SQL Editor. Keep the bucket private. Never expose the service-role key to a browser or mobile client.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
uvicorn app.main:app --reload
```

Open `http://localhost:8000/` for the FitCart interface or `http://localhost:8000/docs` for the interactive API documentation.

## Request

```bash
curl -X POST http://localhost:8000/v1/products/scrape \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.amazon.in/dp/PRODUCT_ID","country":"IN"}'
```

Successful responses contain `data`, `scraped_at`, and `provider`. Unknown product fields are returned as `null` or empty arrays; the model is instructed not to invent missing values.

## Deploy (GitHub Pages UI + Supabase backend)

The production setup has two parts, each deployed by GitHub Actions on every push to `main`:

| Part | Where it runs | Source | Workflow |
| --- | --- | --- | --- |
| FitCart UI | GitHub Pages: `https://jeet9909.github.io/fitcart-scraper-service/` | `app/static/` | `.github/workflows/deploy-pages.yml` |
| API | Supabase Edge Function: `https://<project-ref>.supabase.co/functions/v1/fitcart-api` | `supabase/functions/fitcart-api/` | `.github/workflows/deploy-supabase.yml` |
| Database and storage | Supabase Postgres table `try_on_gallery` plus the private bucket `fitcart-tryons` | `supabase/migrations/` | `.github/workflows/deploy-supabase.yml` |

The Edge Function is a TypeScript port of the FastAPI service with the same routes and response shapes: `/health`, `/ready`, `/v1/products/scrape`, `/v1/sessions/anonymous`, `/v1/try-ons`, and `/v1/gallery`. Supabase injects `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` into it automatically, so the service-role key never has to leave Supabase.

### One-time setup

1. **Create a Supabase project** and copy its project ref (the `xxxx` in `https://xxxx.supabase.co`).
2. **Create a Supabase personal access token** at <https://supabase.com/dashboard/account/tokens>.
3. **In GitHub, open Settings → Secrets and variables → Actions** and add:

   Secrets:
   - `SUPABASE_ACCESS_TOKEN`: the personal access token
   - `SUPABASE_DB_PASSWORD`: the database password (lets the workflow run `supabase db push`)
   - `BRIGHTDATA_API_TOKEN`
   - `GEMINI_API_KEY`
   - `ANONYMOUS_TOKEN_SECRET`: at least 32 random characters, e.g. `openssl rand -hex 32`

   Variables:
   - `SUPABASE_PROJECT_REF`: the project ref
   - Optional: `ALLOWED_PRODUCT_HOSTS`, `GEMINI_IMAGE_MODEL`, `ALLOWED_ORIGINS` (comma-separated; defaults to `https://jeet9909.github.io` plus localhost), and `FITCART_API_BASE` (overrides the API URL the UI calls)
4. **Enable Pages:** Settings → Pages → Build and deployment → Source: **GitHub Actions**.
5. Push to `main`, or run both workflows manually from the **Actions** tab.

The Supabase workflow applies the migrations, sets the function secrets, deploys `fitcart-api` with JWT verification disabled (the function issues and checks its own anonymous tokens), and then smoke-tests `/health`. The Pages workflow publishes `app/static` and writes `static/config.js` so the UI calls the Supabase function.

If you did not set `SUPABASE_DB_PASSWORD`, run `supabase/schema.sql` once in the Supabase SQL Editor instead.

### Manual deploy with the Supabase CLI

```bash
supabase login
supabase link --project-ref <project-ref>
supabase db push
supabase secrets set BRIGHTDATA_API_TOKEN=... GEMINI_API_KEY=... ANONYMOUS_TOKEN_SECRET=...
supabase functions deploy fitcart-api --no-verify-jwt
```

### Test the Edge Function

```bash
cd supabase/functions/fitcart-api
deno test --allow-env --allow-net=127.0.0.1
```

### Docker (alternative)

The FastAPI service can still run as one container that serves both the UI and the API:

```bash
docker build -t fitcart-scraper-service .
docker run --rm -p 8000:8000 --env-file .env fitcart-scraper-service
```

## Production notes

- Set `ALLOWED_PRODUCT_HOSTS=amazon.in,flipkart.com,myntra.com,ajio.com,meesho.com` to restrict accepted URLs.
- Put this service behind FitCart authentication and per-user rate limiting before public launch.
- The endpoint maps provider/timeout failures to HTTP `502` and invalid or unsafe URLs to `400`.
- Product imports consume Bright Data requests and virtual try-ons consume Gemini requests. Monitor both provider dashboards.

## Tests

```bash
pytest
```

Tests use fakes and do not spend OpenAI tokens or Bright Data credits.

## Documentation

- [Bright Data documentation index](https://docs.brightdata.com/llms.txt)
- [Bright Data MCP overview](https://docs.brightdata.com/products/mcp-server/overview.md)
- [Bright Data MCP tools](https://docs.brightdata.com/products/mcp-server/tools.md)
- [OpenAI Responses API](https://platform.openai.com/docs/api-reference/responses)
