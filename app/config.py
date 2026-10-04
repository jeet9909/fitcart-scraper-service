from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


OFFICIAL_PRODUCT_SHARE_HOSTS = ("amzn.in", "fkrt.it")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openai_api_key: SecretStr = SecretStr("")
    brightdata_api_token: SecretStr
    gemini_api_key: SecretStr = SecretStr("")
    gemini_image_model: str = "gemini-3.1-flash-image-preview"  # gemini-2.5-flash-image shuts down on 2 Oct 2026
    gemini_text_model: str = "gemini-2.5-flash"
    face_refine_enabled: bool = Field(default=True, description="Standard pose: a second, edit-only Gemini pass that makes the head match the real person")
    vertex_tryon_enabled: bool = Field(default=True, description="Use Google Vertex AI Virtual Try-On for looks in the person's own pose when a service account is set")
    vertex_tryon_model: str = "virtual-try-on-001"
    vertex_location: str = "us-central1"
    vertex_project_id: str = Field(default="", description="Google Cloud project; defaults to the project in the service account key")
    google_service_account_json: SecretStr = Field(default=SecretStr(""), description="Whole JSON key of a service account with the Vertex AI User role")
    face_match_target: float = Field(default=0.6, description="Identity score (SFace cosine, 0-1) a refined face should reach against the real photo; below it the face is redrawn once more and the closest version is kept")
    face_refine_retries: int = Field(default=1, ge=0, le=3, description="Extra face-refine attempts when the identity score is below face_match_target")
    face_lock_enabled: bool = Field(default=True, description="Blend the person's real face back onto generated try-ons and send a face close-up as an identity reference")
    supabase_url: str = ""
    supabase_service_role_key: SecretStr = SecretStr("")
    supabase_storage_bucket: str = "fitcart-tryons"
    anonymous_token_secret: SecretStr = SecretStr("")
    admin_api_token: SecretStr = SecretStr("")
    admin_emails_csv: str = Field(default="", validation_alias="ADMIN_EMAILS", description="Comma-separated emails that may sign in to the admin dashboard at /admin")
    admin_session_hours: int = Field(default=12, ge=1, le=168)
    cost_gemini_image_inr: float = Field(default=6.40, ge=0, description="Estimated cost of one Gemini image call, for the admin dashboard")
    cost_vertex_tryon_inr: float = Field(default=5.30, ge=0, description="Estimated cost of one Vertex Virtual Try-On call")
    cost_gemini_text_inr: float = Field(default=0.10, ge=0, description="Estimated cost of one Gemini text call (stylist ideas)")
    monthly_fixed_costs_inr: float = Field(default=0, ge=0, description="Hosting, email, scraping and other fixed costs per month, for the admin profit view")
    anonymous_token_days: int = Field(default=30, ge=1, le=365)
    gallery_signed_url_seconds: int = Field(default=3600, ge=60, le=86400)
    max_image_bytes: int = Field(default=10_000_000, ge=100_000, le=20_000_000)
    brightdata_zone: str = Field(default="agent_unlocker", validation_alias="BRIGHTDATA_ZONE")
    openai_model: str = "gpt-4o"
    scrape_timeout_seconds: float = Field(default=60, gt=0, le=180)
    scrape_cache_seconds: int = Field(default=900, ge=0, le=86400, description="Reuse a scraped product for this long; 0 disables the cache")
    max_concurrent_scrapes: int = Field(default=5, ge=1, le=100)
    allowed_product_hosts_csv: str = Field(default="", validation_alias="ALLOWED_PRODUCT_HOSTS")
    unlimited_emails_csv: str = Field(default="", validation_alias="UNLIMITED_EMAILS")
    look_limits_enabled: bool = True
    free_looks_per_month: int = Field(default=2, ge=0, le=100)
    razorpay_key_id: str = ""
    razorpay_key_secret: SecretStr = SecretStr("")
    razorpay_webhook_secret: SecretStr = SecretStr("")
    razorpay_autopay: bool = Field(default=False, description="Bill Plus and Pro as Razorpay subscriptions (UPI AutoPay / card mandates). Needs Subscriptions enabled on the Razorpay account; when off they are one-time prepaid payments for a month or a year")
    razorpay_allow_live: bool = Field(default=False, description="Refuse rzp_live_ keys unless this is set, so test mode cannot turn into real charges by accident")

    def is_unlimited(self, email: str | None) -> bool:
        """Signed-in emails listed in UNLIMITED_EMAILS get unlimited looks."""
        allowed = {item.strip().lower() for item in self.unlimited_emails_csv.split(",") if item.strip()}
        return bool(email) and email.strip().lower() in allowed

    def is_admin(self, email: str | None) -> bool:
        allowed = {item.strip().lower() for item in self.admin_emails_csv.split(",") if item.strip()}
        return bool(email) and email.strip().lower() in allowed

    @property
    def allowed_product_hosts(self) -> tuple[str, ...]:
        configured_hosts = tuple(
            host.strip().lower()
            for host in self.allowed_product_hosts_csv.split(",")
            if host.strip()
        )
        # An empty allowlist accepts any public URL. Official store share links
        # use separate redirect domains, so keep them accepted when a production
        # allowlist is configured.
        if not configured_hosts:
            return ()
        return tuple(dict.fromkeys((*configured_hosts, *OFFICIAL_PRODUCT_SHARE_HOSTS)))

    @property
    def brightdata_mcp_url(self) -> str:
        token = self.brightdata_api_token.get_secret_value()
        return f"https://mcp.brightdata.com/sse?token={token}"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
