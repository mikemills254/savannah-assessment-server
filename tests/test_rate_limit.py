"""Rate limiting against the real Redis: the sliding window, the escalating
blocks, and what the API returns when a caller is blocked."""

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError

from app.core import rate_limit
from app.core.errors import AppError
from app.core.rate_limit import Policy, TooManyRequestsError, check, keys
from app.core.redis import redis_sync
from app.core.security import create_access_token
from app.main import app_error_handler
from tests.conftest import ROOT_URL, auth

pytestmark = pytest.mark.usefixtures("empty_redis")

POLICY = Policy("test", limit=3, window=60)


def hit(policy: Policy = POLICY, identity: str = "a") -> int | None:
    """None if the request is allowed, else its Retry-After in seconds."""
    try:
        check(policy, identity)
    except TooManyRequestsError as exc:
        return exc.retry_after
    return None


def end_block(policy: Policy = POLICY, identity: str = "a") -> None:
    """As if the block had run out."""
    redis_sync.delete(keys(policy, identity)[0])


class TestCheck:
    def test_allows_up_to_the_limit_then_blocks_for_a_minute(self):
        assert [hit() for _ in range(3)] == [None, None, None]
        assert hit() == 60

    def test_each_further_block_is_longer_up_to_30_minutes(self):
        blocks = []
        for _ in range(6):
            assert [hit() for _ in range(3)] == [None, None, None]
            blocks.append(hit())
            end_block()

        assert blocks == [60, 5 * 60, 15 * 60, 30 * 60, 30 * 60, 30 * 60]

    def test_requests_while_blocked_are_refused_without_extending_the_block(self):
        for _ in range(4):
            hit()
        block_key = keys(POLICY, "a")[0]
        before = redis_sync.pttl(block_key)

        assert hit() in (59, 60)
        assert redis_sync.pttl(block_key) <= before

    def test_being_blocked_is_remembered_for_an_hour(self):
        for _ in range(4):
            hit()

        assert 59 * 60_000 < redis_sync.pttl(keys(POLICY, "a")[2]) <= 60 * 60_000

    def test_after_strike_ttl_without_a_block_the_next_one_is_short_again(self):
        policy = Policy("short-memory", limit=1, window=60, strike_ttl=1)
        hit(policy)
        assert hit(policy) == 60
        end_block(policy)

        time.sleep(1.1)
        hit(policy)

        assert hit(policy) == 60

    def test_the_window_slides(self):
        policy = Policy("sliding", limit=2, window=1)
        hit(policy)
        time.sleep(0.6)
        hit(policy)
        time.sleep(0.5)

        # The first request has left the window; the second hasn't.
        assert hit(policy) is None
        assert hit(policy) == 60

    def test_identities_and_policies_are_counted_separately(self):
        for _ in range(4):
            hit(identity="a")

        assert hit(identity="b") is None
        assert hit(Policy("other", limit=3, window=60), "a") is None

    def test_concurrent_requests_cannot_exceed_the_limit(self):
        policy = Policy("burst", limit=10, window=60)

        with ThreadPoolExecutor(max_workers=20) as pool:
            results = list(pool.map(lambda _: hit(policy), range(50)))

        assert results.count(None) == 10

    def test_redis_holds_no_email_or_ip(self):
        hit(identity="203.0.113.7|someone@example.com")

        stored = redis_sync.keys("*")
        assert stored
        assert not any("example.com" in k or "203.0.113.7" in k for k in stored)

    def test_a_new_block_is_logged_once(self, caplog):
        caplog.set_level(logging.WARNING, logger="app.core.rate_limit")

        for _ in range(6):
            hit()

        blocked = [
            r for r in caplog.records if getattr(r, "event", "") == "rate_limit.blocked"
        ]
        assert len(blocked) == 1
        assert (blocked[0].policy, blocked[0].strikes, blocked[0].retry_after) == (
            "test",
            1,
            60,
        )

    def test_lets_requests_through_when_redis_is_down(self, monkeypatch, caplog):
        def redis_down(*args, **kwargs):
            raise ConnectionError("Connection refused")

        monkeypatch.setattr(rate_limit, "_SCRIPT", redis_down)

        assert [hit() for _ in range(5)] == [None] * 5
        assert any(
            getattr(r, "event", "") == "rate_limit.unavailable" for r in caplog.records
        )

    def test_can_be_turned_off(self, monkeypatch):
        monkeypatch.setattr(rate_limit.settings, "rate_limit_enabled", False)

        assert [hit() for _ in range(5)] == [None] * 5


class TestDependency:
    @pytest.fixture
    def api(self) -> TestClient:
        app = FastAPI()
        app.add_exception_handler(AppError, app_error_handler)
        limit = rate_limit.rate_limit(Policy("dependency", limit=2, window=60))

        @app.get("/thing", dependencies=[Depends(limit)])
        def thing() -> dict:
            return {}

        return TestClient(app)

    def test_counts_per_signed_in_user(self, api):
        alice = {"Authorization": f"Bearer {create_access_token(uuid4())}"}
        bob = {"Authorization": f"Bearer {create_access_token(uuid4())}"}

        assert [api.get("/thing", headers=alice).status_code for _ in range(3)] == [
            200,
            200,
            429,
        ]
        assert api.get("/thing", headers=bob).status_code == 200

    def test_counts_per_ip_when_signed_out_or_the_token_is_invalid(self, api):
        api.get("/thing")
        api.get("/thing", headers={"Authorization": "Bearer not-a-token"})

        response = api.get("/thing")

        assert response.status_code == 429
        assert response.headers["Retry-After"] == "60"
        assert response.json() == {
            "detail": "Too many requests. Try again in a minute."
        }


class TestApi:
    def test_api_routes_share_the_limit_but_root_does_not_count(self, client, make):
        client.get(f"{ROOT_URL}/")
        assert redis_sync.keys("rl:{api:*") == []

        assert client.get("/auth/me", headers=auth(make.patient())).status_code == 200
        assert len(redis_sync.keys("rl:{api:*")) == 1

    def test_login_blocks_one_account_after_five_attempts(self, client, make):
        make.patient(email="pat@example.com")
        wrong = {"email": "pat@example.com", "password": "wrong-password"}

        statuses = [
            client.post("/auth/login", json=wrong).status_code for _ in range(6)
        ]
        assert statuses == [401] * 5 + [429]

        # Still blocked however the email is written.
        response = client.post(
            "/auth/login", json={**wrong, "email": " PAT@example.com"}
        )
        assert response.status_code == 429
        assert response.headers["Retry-After"] in ("59", "60")
        # Other accounts from the same IP aren't.
        other = {"email": "other@example.com", "password": "wrong-password"}
        assert client.post("/auth/login", json=other).status_code == 401

    def test_login_blocks_one_ip_after_twenty_attempts_across_accounts(self, client):
        statuses = [
            client.post(
                "/auth/login",
                json={"email": f"user{i}@example.com", "password": "wrong-password"},
            ).status_code
            for i in range(21)
        ]

        assert statuses == [401] * 20 + [429]

    def test_accept_invite_blocks_token_guessing(self, client):
        statuses = [
            client.post(
                "/auth/accept-invite",
                json={"token": "guess", "password": "a-long-password-1"},
            ).status_code
            for _ in range(6)
        ]

        assert 429 not in statuses[:5]
        assert statuses[5] == 429
