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

It intentionally does not contain carts, affiliate redirects, or the FitCart frontend.

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

Open `http://localhost:8000/docs` for the interactive API documentation.

## Request

```bash
curl -X POST http://localhost:8000/v1/products/scrape \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.amazon.in/dp/PRODUCT_ID","country":"IN"}'
```

Successful responses contain `data`, `scraped_at`, and `provider`. Unknown product fields are returned as `null` or empty arrays; the model is instructed not to invent missing values.

## Deploy

Build and run with Docker:

```bash
docker build -t fitcart-scraper-service .
docker run --rm -p 8000:8000 \
  -e OPENAI_API_KEY \
  -e BRIGHTDATA_API_TOKEN \
  fitcart-scraper-service
```

For Render, Railway, Fly.io, or another container platform, deploy the repository with this `Dockerfile`, expose the platform-provided `PORT`, and add the two secrets as environment variables.

## Production notes

- Set `ALLOWED_PRODUCT_HOSTS=amazon.in,flipkart.com,myntra.com,ajio.com,meesho.com` to restrict accepted URLs.
- Put this service behind FitCart authentication and per-user rate limiting before public launch.
- The endpoint maps provider/timeout failures to HTTP `502` and invalid or unsafe URLs to `400`.
- A request may consume both OpenAI tokens and Bright Data credits. Monitor both provider dashboards.

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
