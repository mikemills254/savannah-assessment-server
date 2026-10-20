"""Settings parsing that the deployment depends on."""

import pytest

from app.core.config import Settings

REQUIRED = {
    "redis_url": "redis://localhost:6379/0",
    "jwt_secret_key": "x" * 32,
    "mail_username": "u",
    "mail_password": "p",
    "mail_from": "no-reply@example.com",
    "mail_server": "smtp.invalid",
}


@pytest.mark.parametrize(
    "given",
    [
        "postgres://u:p@db:5432/clinic",
        "postgresql://u:p@db:5432/clinic",
        "postgresql+psycopg://u:p@db:5432/clinic",
    ],
)
def test_database_urls_from_hosts_use_psycopg_3(given):
    settings = Settings(database_url=given, **REQUIRED)

    assert settings.database_url == "postgresql+psycopg://u:p@db:5432/clinic"


def test_cors_origins_are_a_comma_separated_list(monkeypatch):
    monkeypatch.setenv(
        "CORS_ORIGINS", " https://clinic.example.com/ , https://admin.example.com,"
    )

    settings = Settings(database_url="postgresql://u:p@db/c", **REQUIRED)

    assert settings.cors_origins == [
        "https://clinic.example.com",
        "https://admin.example.com",
    ]


def test_the_commit_comes_from_the_host(monkeypatch):
    monkeypatch.delenv("GIT_COMMIT", raising=False)
    monkeypatch.setenv("RENDER_GIT_COMMIT", "abc123")

    settings = Settings(database_url="postgresql://u:p@db/c", **REQUIRED)

    assert settings.git_commit == "abc123"
