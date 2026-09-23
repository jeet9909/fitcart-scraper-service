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
ADMIN_API_TOKEN=...
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

## Deploy (Render API + Supabase storage + GitHub Pages UI)

| Part | Where it runs | Source |
| --- | --- | --- |
| API | Render web service (this `Dockerfile`) | `app/` |
| Gallery database and images | Supabase: table `try_on_gallery` and private bucket `fitcart-tryons` | `supabase/schema.sql` |
| FitCart UI | GitHub Pages: `https://jeet9909.github.io/fitcart-scraper-service/` | `app/static/`, via `.github/workflows/deploy-pages.yml` |

The Render service also serves the UI at its own root URL.

### Render (API)

Configure every variable from `.env.example` in the Render service's **Environment** tab: `BRIGHTDATA_API_TOKEN`, `GEMINI_API_KEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `ANONYMOUS_TOKEN_SECRET`, `ADMIN_API_TOKEN`, and optionally `ALLOWED_PRODUCT_HOSTS`. Render redeploys automatically on every push to `main` when auto-deploy is on. Check `https://<your-service>.onrender.com/health`.

### Supabase (storage)

Run `supabase/schema.sql` once in the Supabase SQL Editor. Keep the bucket private, and never expose the service-role key to a browser.

### GitHub Pages (UI)

1. In GitHub, open **Settings → Secrets and variables → Actions → Variables** and add `FITCART_API_BASE` = your Render URL, e.g. `https://fitcart-scraper-service.onrender.com`.
2. Under **Settings → Pages → Build and deployment**, set **Source** to **GitHub Actions**.
3. Push to `main`, or run **Deploy UI to GitHub Pages** from the Actions tab.

The workflow publishes `app/static` and writes `static/config.js` so the UI calls the Render API. The API's CORS policy already allows `https://jeet9909.github.io`.

### Docker (local or another host)

```bash
docker build -t fitcart-scraper-service .
docker run --rm -p 8000:8000 --env-file .env fitcart-scraper-service
```

## Production notes

- Set `ALLOWED_PRODUCT_HOSTS=amazon.in,flipkart.com,myntra.com,ajio.com,meesho.com` to restrict accepted URLs.
- Put this service behind FitCart authentication and per-user rate limiting before public launch.
- The endpoint maps provider/timeout failures to HTTP `502` and invalid or unsafe URLs to `400`.
- Product imports consume Bright Data requests and virtual try-ons consume Gemini requests. Monitor both provider dashboards.

## Gemini usage check

```bash
curl -H "X-Admin-Token: $ADMIN_API_TOKEN" https://<your-service>.onrender.com/v1/admin/gemini/usage
```

Returns whether the Gemini key and model work, the all-time number of saved try-ons, and requests and tokens counted since the server last started. Gemini API keys cannot read their remaining quota or credit balance, so `remaining_credits` is always `null`; check [Google AI Studio usage](https://aistudio.google.com/usage) and your Cloud billing for that.

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
