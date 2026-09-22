from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


OFFICIAL_PRODUCT_SHARE_HOSTS = ("amzn.in", "fkrt.it")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openai_api_key: SecretStr
    brightdata_api_token: SecretStr
    brightdata_zone: str = Field(default="agent_unlocker", validation_alias="BRIGHTDATA_ZONE")
    openai_model: str = "gpt-4o"
    scrape_timeout_seconds: float = Field(default=60, gt=0, le=180)
    max_concurrent_scrapes: int = Field(default=5, ge=1, le=100)
    allowed_product_hosts_csv: str = Field(default="", validation_alias="ALLOWED_PRODUCT_HOSTS")

    @property
    def allowed_product_hosts(self) -> tuple[str, ...]:
        configured_hosts = tuple(
            host.strip().lower()
            for host in self.allowed_product_hosts_csv.split(",")
            if host.strip()
        )
        # Official store share links use separate redirect domains. Keep these
        # accepted even when a production allowlist is configured.
        return tuple(dict.fromkeys((*configured_hosts, *OFFICIAL_PRODUCT_SHARE_HOSTS)))

    @property
    def brightdata_mcp_url(self) -> str:
        token = self.brightdata_api_token.get_secret_value()
        return f"https://mcp.brightdata.com/sse?token={token}"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
