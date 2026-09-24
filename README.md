# FitCart Product and Virtual Try-On API

Standalone product scraping and Gemini virtual try-on API for FitCart. Product pages are fetched through Bright Data; the scraper reads the page's structured data (JSON-LD, Myntra page state, Amazon price and variation markup) for price, MRP, in-stock and sold-out sizes, colour and fabric, and falls back to Bright Data MCP markdown. Try-on images are generated with Gemini and saved in a private Supabase gallery.

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

Run `supabase/schema.sql` in the Supabase SQL Editor. Run it again after upgrading: it is safe to re-run, and it adds the `wardrobe_items` table and the gallery's outfit columns. Keep the bucket private, and never expose the service-role key to a browser.

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

## Wardrobe and outfit try-on

- **Shopping wardrobe** (`collection=store`): products saved from store links. Paste up to five links from different stores (top, bottom, shoes, jewellery) and import them together, or tap *Save to my shopping wardrobe* on a product.
- **Home wardrobe** (`collection=home`): photos of clothes, footwear and jewellery the user already owns, uploaded by category.
- **Outfit try-on**: pick up to five pieces from either wardrobe and generate one image of the user wearing all of them.
- **AI stylist**: `gemini-2.5-flash` (`GEMINI_TEXT_MODEL`) suggests complete outfits using only the user's own items.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/v1/wardrobe?collection=store\|home` | List saved items |
| `POST` | `/v1/wardrobe` | Add an item (multipart: `collection`, `slot`, `name`, and `image` or `image_url`) |
| `DELETE` | `/v1/wardrobe/{id}` | Remove an item and its photo |
| `POST` | `/v1/wardrobe/suggestions` | `{"collection": "all", "occasion": "office", "count": 3}` → outfit ideas |
| `POST` | `/v1/try-ons` + `outfit_items` | Main product plus up to 4 extra pieces from any store in one try-on. `outfit_items` is a JSON list of `{"slot", "name", "image_url" or "upload", "page_url", "store", "price", "size"}`; `"upload": 0` points at the first `outfit_images` file |
| `POST` | `/v1/try-ons/outfit` | Multipart `person_image` + `item_ids=id1,id2,id3` → saved gallery item |

Wardrobes belong to the anonymous session stored in the browser, like the gallery.

### Pose

Both try-on endpoints take `pose`: `standard` (default) re-poses the person upright and front-facing, arms at the sides, head to toe on a plain studio background so the whole outfit is visible, while keeping their face, hair, glasses, skin tone and body shape. `keep` keeps the pose and background from the uploaded photo. The prompt lives in `tryon_prompt` in `app/tryon.py`.

## Email sign-in and unlimited looks

People can sign in with a one-time email code (the account button in the top bar). Supabase Auth sends the email and checks the code, and the API turns it into a FitCart session tied to that account, so their wardrobe and looks follow them across devices. Endpoints: `POST /v1/auth/email/code`, `POST /v1/auth/email/verify`, `POST /v1/auth/email/link` (for the link in the email), and `GET /v1/me`.

Emails listed in `UNLIMITED_EMAILS` (comma-separated, any case) get unlimited looks. The list is checked on every `GET /v1/me`, so removing an email revokes it on that person's next visit.

Supabase setup:
- **Authentication → Emails → Magic Link** template: add the code, e.g. `<p>Your FitCart code: <strong>{{ .Token }}</strong></p>`, so people can type it in the app.
- **Authentication → URL Configuration**: set Site URL to the GitHub Pages address so the link in the email opens FitCart.
- The built-in Supabase mailer sends only a few emails per hour; add custom SMTP before a wider launch.

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

### "429: You exceeded your current quota"

Gemini image models such as `gemini-2.5-flash-image` have **no free-tier quota**; a key on a project without billing gets `429` with a limit of `0` on every request. Open [Google AI Studio → API keys](https://aistudio.google.com/apikey), choose **Set up billing** for the key's project (or create a key in a project that already has billing), then update `GEMINI_API_KEY` on Render. The API now reports which limit was hit (no quota, daily, or per-minute) and retries a short per-minute limit once automatically.

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
