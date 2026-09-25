import asyncio
from contextlib import asynccontextmanager
from typing import Literal

import hmac

import httpx
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import TypeAdapter, ValidationError

from app.anonymous_auth import create_anonymous_session, create_email_session, verify_session_token
from app import email_auth
from app.billing import Billing
from app.looks import LookLedger, Reservation
from app.config import Settings, get_settings
from app.models import (
    AccountResponse,
    AnonymousSessionResponse,
    BillingConfigResponse,
    CheckoutConfirmRequest,
    CheckoutRequest,
    CheckoutResponse,
    EmailCodeRequest,
    EmailLinkRequest,
    EmailSessionResponse,
    EmailVerifyRequest,
    GalleryItem,
    GalleryResponse,
    GeminiUsageResponse,
    HealthResponse,
    LookBalanceResponse,
    OutfitExtraItem,
    OutfitSuggestionRequest,
    OutfitSuggestionResponse,
    ScrapeRequest,
    ScrapeResponse,
    WardrobeItem,
    WardrobeResponse,
)
from app.scraper import BrightDataScraper, ScrapeProviderError
from app.security import UnsafeUrlError, validate_public_url
from app.tryon import MAX_OUTFIT_PIECES, OutfitPiece, TryOnError, TryOnService, validate_image
from app.wardrobe import SLOT_LABELS, WardrobeService


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    app.state.scraper = BrightDataScraper(settings)
    app.state.tryon = TryOnService(settings)
    app.state.wardrobe = WardrobeService(app.state.tryon)
    yield


app = FastAPI(
    title="FitCart Product and Virtual Try-On API",
    version="0.4.0",
    description="Scrape product details and create private Gemini virtual try-on images.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://jeet9909.github.io",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Admin-Token"],
)

app.mount("/static", StaticFiles(directory="app/static"), name="static")


def get_scraper(request: Request) -> BrightDataScraper:
    return request.app.state.scraper


def get_runtime_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_tryon_service(request: Request) -> TryOnService:
    return request.app.state.tryon


def get_wardrobe_service(request: Request) -> WardrobeService:
    return request.app.state.wardrobe


bearer = HTTPBearer(
    auto_error=False,
    description="Paste the access_token returned by POST /v1/sessions/anonymous. The API also accepts a value beginning with 'Bearer '.",
)


@app.get("/", include_in_schema=False)
async def storefront() -> FileResponse:
    return FileResponse("app/static/index.html")


def get_session_claims(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    settings: Settings = Depends(get_runtime_settings),
) -> dict:
    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Bearer token required")
    token = credentials.credentials.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return verify_session_token(token, settings)


def get_anonymous_user(claims: dict = Depends(get_session_claims)) -> str:
    return claims["sub"]


def get_ledger(settings: Settings = Depends(get_runtime_settings), service: TryOnService = Depends(get_tryon_service)) -> LookLedger:
    return LookLedger(service, settings)


def get_billing(settings: Settings = Depends(get_runtime_settings), ledger: LookLedger = Depends(get_ledger)) -> Billing:
    return Billing(settings, ledger)


async def spend_look(claims: dict = Depends(get_session_claims), ledger: LookLedger = Depends(get_ledger)):
    """Take one look before generating and give it back if the try-on fails for any reason."""
    reservation = await ledger.reserve(claims)
    try:
        yield reservation
    except BaseException:
        await ledger.refund(reservation)
        raise


def require_email(claims: dict = Depends(get_session_claims)) -> dict:
    if not claims.get("email"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": "sign_in_required", "message": "Sign in with your email first."})
    return claims


@app.get("/health", response_model=HealthResponse, tags=["system"])
async def health() -> HealthResponse:
    return HealthResponse()


@app.get("/ready", response_model=HealthResponse, tags=["system"])
async def ready(settings: Settings = Depends(get_runtime_settings)) -> HealthResponse:
    if not settings.brightdata_api_token.get_secret_value():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service is not configured")
    return HealthResponse()


@app.post("/v1/products/scrape", response_model=ScrapeResponse, tags=["products"])
async def scrape_product(
    payload: ScrapeRequest,
    scraper: BrightDataScraper = Depends(get_scraper),
    settings: Settings = Depends(get_runtime_settings),
) -> ScrapeResponse:
    try:
        url = validate_public_url(str(payload.url), settings.allowed_product_hosts)
        return await scraper.scrape(url, payload.country)
    except UnsafeUrlError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ScrapeProviderError as exc:
        response_status = (
            status.HTTP_400_BAD_REQUEST
            if exc.code == "invalid_share_link"
            else status.HTTP_502_BAD_GATEWAY
        )
        raise HTTPException(
            status_code=response_status,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc


@app.post("/v1/sessions/anonymous", response_model=AnonymousSessionResponse, tags=["sessions"])
async def create_session(settings: Settings = Depends(get_runtime_settings)) -> AnonymousSessionResponse:
    return create_anonymous_session(settings)


@app.post("/v1/auth/email/code", status_code=204, tags=["sessions"])
async def send_email_code(body: EmailCodeRequest, settings: Settings = Depends(get_runtime_settings)) -> None:
    """Email a one-time sign-in code (and link) through Supabase Auth."""
    await email_auth.send_code(email_auth.normalize_email(body.email), settings)


@app.post("/v1/auth/email/verify", response_model=EmailSessionResponse, tags=["sessions"])
async def verify_email_code(body: EmailVerifyRequest, settings: Settings = Depends(get_runtime_settings)) -> EmailSessionResponse:
    user_id, email = await email_auth.verify_code(email_auth.normalize_email(body.email), body.code, settings)
    return create_email_session(user_id, email, settings)


@app.post("/v1/auth/email/link", response_model=EmailSessionResponse, tags=["sessions"])
async def exchange_email_link(body: EmailLinkRequest, settings: Settings = Depends(get_runtime_settings)) -> EmailSessionResponse:
    """Turn the access token from a clicked sign-in link into a FitCart session."""
    user_id, email = await email_auth.user_from_link_token(body.access_token, settings)
    return create_email_session(user_id, email, settings)


@app.get("/v1/me", response_model=AccountResponse, tags=["sessions"])
async def current_account(claims: dict = Depends(get_session_claims), settings: Settings = Depends(get_runtime_settings)) -> AccountResponse:
    """Who this session belongs to and whether it has unlimited looks (checked against UNLIMITED_EMAILS on every call)."""
    email = claims.get("email")
    return AccountResponse(user_id=claims["sub"], email=email, unlimited=settings.is_unlimited(email))


@app.get("/v1/looks/balance", response_model=LookBalanceResponse, tags=["looks and billing"])
async def look_balance(claims: dict = Depends(get_session_claims), ledger: LookLedger = Depends(get_ledger)) -> LookBalanceResponse:
    """Looks left for this account. Try-ons need an email sign-in; each one spends a look."""
    return await ledger.balance(claims)


@app.get("/v1/billing/config", response_model=BillingConfigResponse, tags=["looks and billing"])
async def billing_config(billing: Billing = Depends(get_billing)) -> BillingConfigResponse:
    return BillingConfigResponse(enabled=billing.enabled, test_mode=billing.test_mode)


@app.post("/v1/billing/checkout", response_model=CheckoutResponse, tags=["looks and billing"])
async def create_checkout(body: CheckoutRequest, claims: dict = Depends(require_email), billing: Billing = Depends(get_billing)) -> CheckoutResponse:
    """Start Stripe Checkout for a pass or plan; the browser is sent to the returned URL."""
    return CheckoutResponse(url=await billing.create_checkout(claims["sub"], claims["email"], body.plan, body.billing))


@app.post("/v1/billing/confirm", response_model=LookBalanceResponse, tags=["looks and billing"])
async def confirm_checkout(
    body: CheckoutConfirmRequest,
    claims: dict = Depends(require_email),
    billing: Billing = Depends(get_billing),
    ledger: LookLedger = Depends(get_ledger),
) -> LookBalanceResponse:
    """Add the looks from a finished Checkout when the buyer returns; safe to repeat and safe alongside the webhook."""
    await billing.confirm(claims["sub"], body.session_id)
    return await ledger.balance(claims)


@app.post("/v1/billing/webhook", tags=["looks and billing"], include_in_schema=False)
async def stripe_webhook(request: Request, billing: Billing = Depends(get_billing)) -> dict:
    await billing.handle_webhook(await request.body(), request.headers.get("stripe-signature"))
    return {"received": True}


@app.post("/v1/try-ons", response_model=GalleryItem, tags=["virtual try-on"])
async def create_tryon(
    person_image: UploadFile = File(..., description="Front-facing, full-body user photo"),
    product_image: UploadFile | None = File(None, description="Direct product image upload"),
    product_page_url: str | None = Form(
        None,
        description="Product page/share URL. Scraped for its image unless product_image_url is also sent, in which case it is only saved with the result",
    ),
    product_image_url: str | None = Form(None, description="Direct public product image URL, e.g. image_urls[0] from /v1/products/scrape"),
    category: str = Form("clothing"),
    product_name: str | None = Form(None, max_length=200, description="Product title, helps the model pick the right garment from the product photo"),
    country: str = Form("IN"),
    pose: Literal["standard", "keep"] = Form("standard", description="standard: upright front-facing catalogue pose with the whole outfit visible; keep: the pose from the photo"),
    outfit_items: str | None = Form(
        None,
        description='JSON list of up to 4 extra pieces worn with this product, e.g. [{"slot": "footwear", "name": "White sneakers", "image_url": "https://..."}]. Use "upload": 0 to point at outfit_images[0].',
    ),
    outfit_images: list[UploadFile] | None = File(None, description="Photos for extra pieces that have no image URL"),
    look: Reservation = Depends(spend_look),
    user_id: str = Depends(get_anonymous_user),
    settings: Settings = Depends(get_runtime_settings),
    scraper: BrightDataScraper = Depends(get_scraper),
    service: TryOnService = Depends(get_tryon_service),
) -> GalleryItem:
    try:
        service.ensure_configured()
        extras = _parse_outfit_items(outfit_items, len(outfit_images or []))
        # A product_page_url sent with product_image_url is the listing the image came from, not a second source.
        page_is_source = product_page_url is not None and product_image_url is None
        sources = sum(value is not None for value in (product_image, product_image_url)) + page_is_source
        if sources != 1:
            raise TryOnError("Provide exactly one product source: product_image, product_page_url, or product_image_url", 400)
        person = validate_image(await person_image.read(), person_image.content_type, settings.max_image_bytes)
        source_url: str | None = None
        if product_image is not None:
            product = validate_image(await product_image.read(), product_image.content_type, settings.max_image_bytes)
            product_source = "upload"
        elif product_image_url is not None:
            image_url = validate_public_url(product_image_url)
            source_url = validate_public_url(product_page_url, settings.allowed_product_hosts) if product_page_url else image_url
            product = await service.fetch_image(image_url)
            product_source = "image_url"
        else:
            source_url = validate_public_url(product_page_url or "", settings.allowed_product_hosts)
            scraped = await scraper.scrape(source_url, country.upper())
            if not scraped.data.image_urls:
                raise TryOnError("The scraped product page did not provide a usable product image; upload the product image directly", 422)
            product = await service.fetch_image(validate_public_url(scraped.data.image_urls[0]))
            product_source = "scraped_url"
        name = product_name.strip()[:200] if product_name and product_name.strip() else None
        category = category.strip()[:80] or "clothing"
        if not extras:
            result = await service.generate(person, product, category, product_name=name, pose=pose)
            return await service.save(user_id, person, product, result, category, product_source, source_url)

        async def extra_image(item: OutfitExtraItem) -> tuple[bytes, str, str]:
            if item.upload is not None:
                upload = (outfit_images or [])[item.upload]
                return validate_image(await upload.read(), upload.content_type, settings.max_image_bytes)
            return await service.fetch_image(validate_public_url(item.image_url or ""))

        images = await asyncio.gather(*(extra_image(item) for item in extras))
        pieces = [OutfitPiece(image=product, category=category, label=name)]
        pieces += [OutfitPiece(image=image, category=SLOT_LABELS[item.slot], label=item.name.strip() or None) for item, image in zip(extras, images)]
        result = await service.generate_outfit(person, pieces, pose=pose)
        summary = [{"slot": None, "category": category, "name": name, "product_url": source_url}]
        summary += [
            {"slot": item.slot, "name": item.name, "store": item.store, "price": item.price, "size": item.size,
             "product_url": validate_public_url(item.page_url) if item.page_url else None}
            for item in extras
        ]
        outfit_category = " + ".join([category, *(item.slot for item in extras)])[:80]
        return await service.save(user_id, person, product, result, outfit_category, product_source, source_url, items=summary)
    except UnsafeUrlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ScrapeProviderError as exc:
        raise HTTPException(status_code=502, detail={"code": exc.code, "message": str(exc)}) from exc
    except TryOnError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Could not download the product image") from exc


def _parse_outfit_items(raw: str | None, upload_count: int) -> list[OutfitExtraItem]:
    if not raw or not raw.strip():
        return []
    try:
        items = TypeAdapter(list[OutfitExtraItem]).validate_json(raw)
    except ValidationError as exc:
        raise TryOnError(f"outfit_items is not valid: {exc.errors()[0].get('msg', 'invalid value')}", 400) from exc
    if len(items) > MAX_OUTFIT_PIECES - 1:
        raise TryOnError(f"Add at most {MAX_OUTFIT_PIECES - 1} extra pieces to one try-on", 400)
    for item in items:
        if (item.image_url is None) == (item.upload is None):
            raise TryOnError("Each extra piece needs exactly one of image_url or upload", 400)
        if item.upload is not None and item.upload >= upload_count:
            raise TryOnError("An extra piece points at a photo that was not uploaded", 400)
    return items


@app.post("/v1/try-ons/outfit", response_model=GalleryItem, tags=["virtual try-on"])
async def create_outfit_tryon(
    person_image: UploadFile = File(..., description="Front-facing, full-body user photo"),
    item_ids: str = Form(..., description="Comma-separated wardrobe item ids (1 to 5), e.g. a top, a bottom and shoes"),
    pose: Literal["standard", "keep"] = Form("standard", description="standard: upright front-facing catalogue pose with the whole outfit visible; keep: the pose from the photo"),
    look: Reservation = Depends(spend_look),
    user_id: str = Depends(get_anonymous_user),
    settings: Settings = Depends(get_runtime_settings),
    service: TryOnService = Depends(get_tryon_service),
    wardrobe: WardrobeService = Depends(get_wardrobe_service),
) -> GalleryItem:
    """Try on a whole outfit built from wardrobe items, which can come from different stores and from your own clothes."""
    try:
        service.ensure_configured()
        person = validate_image(await person_image.read(), person_image.content_type, settings.max_image_bytes)
        pieces, summary = await wardrobe.outfit_pieces(user_id, [item.strip() for item in item_ids.split(",") if item.strip()])
        result = await service.generate_outfit(person, pieces, pose=pose)
        category = " + ".join(item["slot"] for item in summary)[:80]
        product_url = next((item["product_url"] for item in summary if item.get("product_url")), None)
        return await service.save(user_id, person, pieces[0].image, result, category, "wardrobe", product_url, items=summary)
    except TryOnError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Could not reach the image or storage service") from exc


@app.get("/v1/wardrobe", response_model=WardrobeResponse, tags=["wardrobe"])
async def list_wardrobe(
    collection: str | None = None,
    user_id: str = Depends(get_anonymous_user),
    service: TryOnService = Depends(get_tryon_service),
    wardrobe: WardrobeService = Depends(get_wardrobe_service),
) -> WardrobeResponse:
    """List saved items. collection=store for products saved from shops, collection=home for clothes you own."""
    if collection not in (None, "store", "home"):
        raise HTTPException(status_code=400, detail="collection must be store or home")
    try:
        service.ensure_configured()
        return WardrobeResponse(items=await wardrobe.list_items(user_id, collection))
    except TryOnError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@app.post("/v1/wardrobe", response_model=WardrobeItem, tags=["wardrobe"])
async def add_wardrobe_item(
    collection: str = Form(..., description="store or home"),
    slot: str = Form(..., description="top, bottom, dress, outerwear, footwear, jewelry, accessory or other"),
    name: str = Form("", max_length=200),
    image: UploadFile | None = File(None, description="Photo of the item"),
    image_url: str | None = Form(None, description="Public product image URL, e.g. image_urls[0] from /v1/products/scrape"),
    brand: str | None = Form(None, max_length=120),
    color: str | None = Form(None, max_length=80),
    price: float | None = Form(None, ge=0, le=10_000_000),
    currency: str | None = Form(None, max_length=3),
    sizes: str | None = Form(None, max_length=400, description="Comma-separated sizes listed by the store"),
    selected_size: str | None = Form(None, max_length=40),
    store: str | None = Form(None, max_length=120),
    product_url: str | None = Form(None, max_length=2000),
    notes: str | None = Form(None, max_length=500),
    user_id: str = Depends(get_anonymous_user),
    settings: Settings = Depends(get_runtime_settings),
    service: TryOnService = Depends(get_tryon_service),
    wardrobe: WardrobeService = Depends(get_wardrobe_service),
) -> WardrobeItem:
    if collection not in ("store", "home"):
        raise HTTPException(status_code=400, detail="collection must be store or home")
    if (image is None) == (image_url is None):
        raise HTTPException(status_code=400, detail="Provide exactly one of image or image_url")
    try:
        service.ensure_configured()
        if image is not None:
            picture = validate_image(await image.read(), image.content_type, settings.max_image_bytes)
        else:
            picture = await service.fetch_image(validate_public_url(image_url or ""))
        return await wardrobe.add(
            user_id, collection, slot, name, picture,
            brand=brand, color=color, price=price, currency=currency.upper() if currency else None,
            sizes=[size.strip()[:40] for size in (sizes or "").split(",") if size.strip()], selected_size=selected_size,
            store=store, product_url=validate_public_url(product_url) if product_url else None,
            source_image_url=image_url, notes=notes,
        )
    except UnsafeUrlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TryOnError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Could not download the product image") from exc


@app.delete("/v1/wardrobe/{item_id}", status_code=204, tags=["wardrobe"])
async def delete_wardrobe_item(
    item_id: str,
    user_id: str = Depends(get_anonymous_user),
    service: TryOnService = Depends(get_tryon_service),
    wardrobe: WardrobeService = Depends(get_wardrobe_service),
) -> None:
    try:
        service.ensure_configured()
        await wardrobe.delete(user_id, item_id)
    except TryOnError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@app.post("/v1/wardrobe/suggestions", response_model=OutfitSuggestionResponse, tags=["wardrobe"])
async def suggest_outfits(
    payload: OutfitSuggestionRequest,
    user_id: str = Depends(get_anonymous_user),
    service: TryOnService = Depends(get_tryon_service),
    wardrobe: WardrobeService = Depends(get_wardrobe_service),
) -> OutfitSuggestionResponse:
    """Ask the AI stylist for complete outfits made only from the user's wardrobe items."""
    try:
        service.ensure_configured()
        occasion = payload.occasion.strip() if payload.occasion else None
        return OutfitSuggestionResponse(outfits=await wardrobe.suggest(user_id, payload.collection, occasion, payload.count))
    except TryOnError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Could not reach the stylist service") from exc


@app.get("/v1/gallery", response_model=GalleryResponse, tags=["virtual try-on"])
async def get_gallery(
    user_id: str = Depends(get_anonymous_user),
    service: TryOnService = Depends(get_tryon_service),
) -> GalleryResponse:
    try:
        service.ensure_configured()
        return GalleryResponse(items=await service.list_gallery(user_id))
    except TryOnError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


def require_admin(
    x_admin_token: str | None = Header(None, description="Must match the ADMIN_API_TOKEN environment variable"),
    settings: Settings = Depends(get_runtime_settings),
) -> None:
    expected = settings.admin_api_token.get_secret_value()
    if not expected:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="ADMIN_API_TOKEN is not configured")
    if not x_admin_token or not hmac.compare_digest(x_admin_token, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid admin token")


@app.get("/v1/admin/gemini/usage", response_model=GeminiUsageResponse, tags=["system"], dependencies=[Depends(require_admin)])
async def gemini_usage(service: TryOnService = Depends(get_tryon_service)) -> GeminiUsageResponse:
    """Check the Gemini key and model, and report usage recorded by this server.

    Google does not let an API key read its remaining quota or credit balance; see quota_dashboard_url for that.
    """
    return await service.gemini_usage()
