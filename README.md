# FitCart Scraper Service

Standalone product scraping API for FitCart. It uses the OpenAI Responses API to connect to the hosted Bright Data MCP server and returns a stable, validated product schema.

## What this repository contains

- `POST /v1/products/scrape` for a single product-share URL
- Bright Data MCP integration through the OpenAI Python SDK
- Normalized price, inventory, images, variants, rating, seller, and product metadata
- Public-URL validation to block localhost/private-network targets
- Concurrency and timeout controls
- Docker deployment and automated tests

It intentionally does **not** contain image generation, authentication, carts, affiliate redirects, or the FitCart frontend.

## Configure

Create a Bright Data API token and an OpenAI API key. Copy `.env.example` to `.env` and set:

```env
OPENAI_API_KEY=...
BRIGHTDATA_API_TOKEN=...
OPENAI_MODEL=gpt-4o
```

Never commit `.env` or paste secrets into source files.

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
