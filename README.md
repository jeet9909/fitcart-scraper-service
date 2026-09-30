# FitCart Product and Virtual Try-On API

Standalone product scraping and Gemini virtual try-on API for FitCart. Product pages are fetched through Bright Data; the scraper reads the page's structured data (JSON-LD, Myntra page state, Amazon price and variation markup) for price, MRP, in-stock and sold-out sizes, colour and fabric, and falls back to Bright Data MCP markdown. Try-on images are generated with Gemini and saved in a private Supabase gallery.

## What this repository contains

- `POST /v1/products/scrape` for a single product-share URL
- `POST /v1/sessions/anonymous` for a private anonymous gallery token
- `POST /v1/try-ons` with a full-body photo plus a product upload, image URL, or scraped product-page URL
- `GET /v1/gallery` for that anonymous user's private gallery
- `POST /v1/try-ons/{id}/spin` (Pro) to turn a saved look into a 360° view: Gemini draws the right side, back and left side, stored next to the look as `spin_paths` and returned as `spin_image_urls` (front first). It uses one look; a look that already has one is returned free
- `POST /v1/try-ons/{id}/poses` `{"pose": "street-walk"}` (Plus and Pro) to redraw a saved look in a social-ready 4:5 pose. Plus has street-walk, pockets and over-shoulder; Pro has all 8 (see `SOCIAL_POSES` in `app/tryon.py`). One look per new pose; stored as `pose_shots` and returned as `pose_images`
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

Both try-on endpoints take `pose`: `standard` (default) re-poses the person upright and front-facing, arms at the sides, head to toe on a plain studio background so the whole outfit is visible, while keeping their face, hair, glasses, skin tone and body proportions. `keep` keeps the pose and background from the uploaded photo. The prompt lives in `tryon_prompt` in `app/tryon.py`; identity and body rules come first because the model weighs early instructions most.

### Keeping the real face

Image models redraw the face whenever they re-pose a person, and tend to slim it. Each try-on sends a close-up of the person's face (cut from the full-resolution upload) as an extra identity reference, with the identity and body-proportion rules first in the prompt.

- **Standard pose:** a second, edit-only Gemini pass (`FACE_REFINE_PROMPT` in `app/tryon.py`) takes the first result plus the face close-up and the original photo and changes only the head: face shape and width, jaw, beard, glasses, hairline and head size relative to the shoulders. Pose, body, clothes and background stay. This is two Gemini calls, so roughly double the cost and 10-15 s more; if the second pass fails the first image is kept. `FACE_REFINE_ENABLED=false` turns it off.
- **Keep pose:** the head barely moves, so face lock pastes the real eyebrows, eyes, nose and mouth onto the result with Poisson blending (OpenCV YuNet landmarks, `app/identity.py`). It skips itself when the faces do not line up.

Pixel pasting is not used for standard pose: onto a head the model drew slimmer, real features look mismatched at the edges. `FACE_LOCK_ENABLED=false` turns off the face close-up and face lock.

## Accounts and unlimited looks

People create an account with **email and password** (the account button in the top bar: Log in / Create account). Accounts live in Supabase Auth: the API creates them through the admin API as already confirmed, so no email is sent, and checks passwords with Supabase's password sign-in; FitCart never stores passwords. Eight wrong passwords for one email lock it for 15 minutes. Endpoints: `POST /v1/auth/signup`, `POST /v1/auth/login`, `GET /v1/me`.

Accounts made earlier with an email code have no password. Give them one with `POST /v1/admin/users/password` (header `X-Admin-Token: <ADMIN_API_TOKEN>`, body `{"email": ..., "password": ...}`), for example from `/docs`; it creates the account if it does not exist.

Emails listed in `UNLIMITED_EMAILS` (comma-separated, any case) get unlimited looks. The list is checked on every `GET /v1/me`, so removing an email revokes it on that person's next visit. Because sign-up does not confirm the email address, create the accounts for unlimited emails yourself (with the admin endpoint) before anyone else can register them.

## Looks and payments

Try-ons need an email sign-in. Each signed-in account gets `FREE_LOOKS_PER_MONTH` looks (default 3) every calendar month, India time; passes and plans bought through Razorpay add more. The API spends a look before generating and gives it back if the try-on fails. Counting happens in Postgres (`consume_look` in `supabase/schema.sql`), so parallel requests cannot overspend. Emails in `UNLIMITED_EMAILS` skip the count. Until the schema has been run, limits are not enforced and the API logs a warning.

| Plan | Price (incl. GST) | Looks |
|---|---|---|
| Occasion Pass | ₹129 once | 10, valid 7 days |
| Plus | ₹349/month or ₹3,299/year | 25 every month |
| Pro | ₹799/month or ₹7,499/year | 60 every month |

Endpoints: `GET /v1/looks/balance`, `GET /v1/billing/config`, `POST /v1/billing/checkout` (creates a Razorpay order for the pass or a subscription for a plan), `POST /v1/billing/confirm` (verifies the checkout signature and adds looks), and `POST /v1/billing/webhook` (Razorpay).

Razorpay setup (test mode):
1. Razorpay Dashboard in **Test Mode** → Account & Settings → API Keys → Generate Test Key. Put them in Render as `RAZORPAY_KEY_ID` (`rzp_test_...`) and `RAZORPAY_KEY_SECRET`. Plans for Plus and Pro are created in Razorpay automatically on the first purchase.
2. Account & Settings → Webhooks → Add New Webhook: URL `https://<your-render-service>.onrender.com/v1/billing/webhook`, a secret of your choice, and the events `order.paid` and `subscription.charged`. Put the same secret in Render as `RAZORPAY_WEBHOOK_SECRET`. Renewals need the webhook; first purchases also work through the checkout callback.
3. Test payments: UPI ID `success@razorpay`, or Netbanking → any bank → Success.

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

Gemini image models such as `gemini-3.1-flash-image-preview` (default; `gemini-2.5-flash-image` shut down on 2 October 2026) have **no free-tier quota**; a key on a project without billing gets `429` with a limit of `0` on every request. Open [Google AI Studio → API keys](https://aistudio.google.com/apikey), choose **Set up billing** for the key's project (or create a key in a project that already has billing), then update `GEMINI_API_KEY` on Render. The API now reports which limit was hit (no quota, daily, or per-minute) and retries a short per-minute limit once automatically.

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
