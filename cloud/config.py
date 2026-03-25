"""Central API configuration — loaded from environment variables."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Database
    database_url: str = "postgresql+asyncpg://davyjones:davyjones@localhost:5432/davyjones"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # JWT auth
    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24 * 7  # 7 days

    # OAuth providers
    github_client_id: str = ""
    github_client_secret: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""

    # Stripe
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_pro: str = ""  # Stripe Price ID for Pro plan
    stripe_price_team: str = ""  # Stripe Price ID for Team plan

    # Forgejo (hosted git)
    forgejo_url: str = "http://forgejo.davyjones-system:3000"
    forgejo_admin_token: str = ""

    # Kubernetes
    k8s_namespace_prefix: str = "dj-"

    # App
    api_url: str = "https://api.davyjones.cloud"
    cors_origins: list[str] = ["*"]

    class Config:
        env_prefix = "DAVYJONES_"
        env_file = ".env"


settings = Settings()
