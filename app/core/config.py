from pydantic_settings import BaseSettings, SettingsConfigDict


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
    frontend_url: str = "http://localhost:5173"


settings = Settings()
