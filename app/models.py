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
    sizes: list[str] = Field(default_factory=list)
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
    created_at: datetime


class GalleryResponse(BaseModel):
    items: list[GalleryItem]
