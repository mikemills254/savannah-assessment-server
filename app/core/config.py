from typing import Annotated

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    redis_url: str
    jwt_secret_key: str
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60
    invite_token_expire_hours: int = 72
    # OAuth client id from Google Cloud Console. Unset disables Google sign-in.
    google_client_id: str | None = None

    mail_username: str
    mail_password: str
    mail_from: str
    mail_from_name: str = "Clinic"
    mail_server: str
    mail_port: int = 587
    # Base URL of the web client; invite emails link to <frontend_url>/accept-invite.
    frontend_url: str

    # Browser origins allowed to call the API, comma-separated, e.g.
    # "https://clinic.example.com,https://staging.clinic.example.com". Empty
    # allows none, which is fine locally where Vite proxies the API.
    cors_origins: Annotated[list[str], NoDecode] = []

    # The deployed commit, shown by /health so a deploy can check it's live.
    # Coolify sets SOURCE_COMMIT; elsewhere set GIT_COMMIT.
    git_commit: str | None = Field(
        default=None,
        validation_alias=AliasChoices("GIT_COMMIT", "SOURCE_COMMIT"),
    )

    @field_validator("git_commit")
    @classmethod
    def _empty_commit_is_unknown(cls, value: str | None) -> str | None:
        # The image bakes in an empty SOURCE_COMMIT when built without one.
        return value or None

    @field_validator("database_url")
    @classmethod
    def _use_psycopg(cls, value: str) -> str:
        # Hosts hand out postgres:// or postgresql:// URLs, which SQLAlchemy
        # maps to psycopg2; this app ships psycopg 3.
        for prefix in ("postgres://", "postgresql://"):
            if value.startswith(prefix):
                return "postgresql+psycopg://" + value.removeprefix(prefix)
        return value

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [o.strip().rstrip("/") for o in value.split(",") if o.strip()]
        return value


settings = Settings()
