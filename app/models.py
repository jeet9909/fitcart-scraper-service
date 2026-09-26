from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl


class ScrapeRequest(BaseModel):
    url: HttpUrl
    country: str = Field(default="IN", min_length=2, max_length=2, pattern=r"^[A-Z]{2}$")


class Money(BaseModel):
    amount: float | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")


class ProductData(BaseModel):
    source_url: str
    store: str | None = None
    external_id: str | None = None
    title: str
    brand: str | None = None
    description: str | None = None
    category: str | None = None
    price: Money
    original_price: Money | None = None
    discount_percent: float | None = Field(default=None, ge=0, le=100)
    availability: Literal["in_stock", "out_of_stock", "unknown"] = "unknown"
    rating: float | None = Field(default=None, ge=0, le=5)
    review_count: int | None = Field(default=None, ge=0)
    image_urls: list[str] = Field(default_factory=list)
    colors: list[str] = Field(default_factory=list)
    sizes: list[str] = Field(default_factory=list, description="Sizes the store lists as in stock, or all listed sizes when stock is unknown")
    unavailable_sizes: list[str] = Field(default_factory=list, description="Sizes the store lists as sold out")
    outfit_slot: Literal["top", "bottom", "dress", "outerwear", "footwear", "jewelry", "accessory", "other"] | None = None
    material: str | None = None
    seller: str | None = None


class ScrapeResponse(BaseModel):
    data: ProductData
    scraped_at: datetime
    provider: Literal["brightdata_mcp"] = "brightdata_mcp"


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


class AnonymousSessionResponse(BaseModel):
    anonymous_user_id: str
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_at: datetime


class EmailSessionResponse(AnonymousSessionResponse):
    email: str
    unlimited: bool = False


class EmailCodeRequest(BaseModel):
    email: str = Field(max_length=254)


class EmailVerifyRequest(BaseModel):
    email: str = Field(max_length=254)
    code: str = Field(max_length=20)


class EmailLinkRequest(BaseModel):
    access_token: str = Field(min_length=20, max_length=8000)


class AccountResponse(BaseModel):
    user_id: str
    email: str | None = None
    unlimited: bool = False


class LookGrantSummary(BaseModel):
    kind: Literal["free", "pass", "plus", "pro", "bonus"]
    remaining: int
    total: int
    expires_at: datetime


class LookBalanceResponse(BaseModel):
    signed_in: bool
    unlimited: bool = False
    enforced: bool = True
    remaining: int | None = Field(default=None, description="Looks left right now; null when unlimited or not signed in")
    plan: Literal["free", "pass", "plus", "pro"] = "free"
    free_looks_per_month: int
    grants: list[LookGrantSummary] = Field(default_factory=list)


class CheckoutRequest(BaseModel):
    plan: Literal["pass", "plus", "pro"]
    billing: Literal["monthly", "yearly"] = "monthly"


class CheckoutResponse(BaseModel):
    """Options for the Razorpay checkout window: an order_id for a pass or a subscription_id for a plan."""
    key_id: str
    name: str
    description: str
    email: str
    currency: str
    amount: int
    order_id: str | None = None
    subscription_id: str | None = None


class CheckoutConfirmRequest(BaseModel):
    razorpay_payment_id: str = Field(pattern=r"^pay_[A-Za-z0-9]+$", max_length=64)
    razorpay_signature: str = Field(pattern=r"^[a-f0-9]{64}$")
    razorpay_order_id: str | None = Field(default=None, pattern=r"^order_[A-Za-z0-9]+$", max_length=64)
    razorpay_subscription_id: str | None = Field(default=None, pattern=r"^sub_[A-Za-z0-9]+$", max_length=64)


class BillingConfigResponse(BaseModel):
    enabled: bool
    test_mode: bool
    provider: Literal["razorpay"] = "razorpay"


class GalleryItem(BaseModel):
    id: str
    anonymous_user_id: str
    category: str
    product_source: str
    product_url: str | None = None
    person_image_url: str
    product_image_url: str
    result_image_url: str
    model: str
    items: list[dict] = Field(default_factory=list, description="Wardrobe items worn in an outfit try-on")
    created_at: datetime


class GalleryResponse(BaseModel):
    items: list[GalleryItem]


class GeminiUsageSinceStart(BaseModel):
    since: datetime
    requests: int
    succeeded: int
    failed: int
    prompt_tokens: int
    output_tokens: int
    total_tokens: int
    last_error: str | None = None


class GeminiUsageResponse(BaseModel):
    model: str
    key_valid: bool
    model_available: bool
    check_message: str | None = None
    remaining_credits: None = Field(
        default=None,
        description="Always null: Gemini API keys cannot read their remaining quota or billing balance. Check Google AI Studio.",
    )
    quota_dashboard_url: str = "https://aistudio.google.com/usage"
    total_saved_tryons: int | None = Field(default=None, description="Successful try-ons saved in the Supabase gallery, all time")
    since_server_start: GeminiUsageSinceStart


OutfitSlot = Literal["top", "bottom", "dress", "outerwear", "footwear", "jewelry", "accessory", "other"]
WardrobeCollection = Literal["store", "home"]


class WardrobeItem(BaseModel):
    id: str
    collection: WardrobeCollection = Field(description="store: products saved from shops; home: clothes you already own")
    slot: OutfitSlot
    name: str
    brand: str | None = None
    color: str | None = None
    price: float | None = None
    currency: str | None = None
    sizes: list[str] = Field(default_factory=list)
    selected_size: str | None = None
    store: str | None = None
    product_url: str | None = None
    notes: str | None = None
    image_url: str
    created_at: datetime


class WardrobeResponse(BaseModel):
    items: list[WardrobeItem]


class OutfitSuggestionRequest(BaseModel):
    collection: Literal["store", "home", "all"] = "all"
    occasion: str | None = Field(default=None, max_length=120, description="e.g. office, wedding, casual weekend")
    count: int = Field(default=3, ge=1, le=5)


class OutfitSuggestion(BaseModel):
    title: str
    reason: str
    item_ids: list[str]


class OutfitSuggestionResponse(BaseModel):
    outfits: list[OutfitSuggestion]


class OutfitExtraItem(BaseModel):
    """An extra piece worn with the main product in one try-on, e.g. shoes from another store."""

    slot: OutfitSlot
    name: str = Field(default="", max_length=200)
    image_url: str | None = Field(default=None, max_length=2000, description="Public product image URL")
    upload: int | None = Field(default=None, ge=0, le=3, description="Index into the outfit_images uploads")
    page_url: str | None = Field(default=None, max_length=2000)
    store: str | None = Field(default=None, max_length=120)
    price: float | None = Field(default=None, ge=0)
    size: str | None = Field(default=None, max_length=40)
