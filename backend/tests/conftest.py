import json
import re

import pytest

from app.crypto import ephemeral_cipher
from app.intake import RISK_SYSTEM
from app.llm import SafeLLM
from app.repository import InMemoryRepo


class RoutingLLM:
    """Answers by purpose rather than call order: low risk for risk questions,
    and for replies, a well-behaved answer: the first sentence of source [S1],
    cited. Pass `answer` to script a specific (e.g. badly behaved) reply."""

    def __init__(self, answer: str | None = None):
        self.answer = answer
        self.prompts: list[dict] = []

    def complete(self, system: str, user: str, timeout_s: float = 10.0, json_mode: bool = False) -> str:
        self.prompts.append({"system": system, "user": user, "json_mode": json_mode})
        if system == RISK_SYSTEM:
            return json.dumps({"risk_level": "low", "reason": "test", "confidence": "high"})
        if self.answer is not None:
            return self.answer
        source = re.search(r"\[S1\] \(.*?\)\n(.+?)(?:\n\n\[S|\Z)", system, re.S)
        if not source:
            return "I don't have that information."
        first_sentence = re.split(r"(?<=[.!?])\s+", source.group(1).strip())[0]
        return f"{first_sentence} [S1]"


@pytest.fixture
def repo():
    return InMemoryRepo()


@pytest.fixture
def cipher():
    return ephemeral_cipher()


@pytest.fixture
def model():
    return RoutingLLM()


@pytest.fixture
def llm(model):
    return SafeLLM(model)


TEST_JWT_SECRET = "test-secret-at-least-32-bytes-long!!"
TEST_META_SECRET = "test-meta-app-secret"


def make_token(sub: str, anonymous: bool = False, email: str | None = None, minutes: int = 60) -> str:
    """A token shaped like Supabase's access tokens, signed with the test secret."""
    import time

    import jwt
    claims = {"sub": sub, "aud": "authenticated", "role": "authenticated",
              "exp": int(time.time()) + minutes * 60, "is_anonymous": anonymous}
    if email:
        claims["email"] = email
    return jwt.encode(claims, TEST_JWT_SECRET, algorithm="HS256")


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def api(repo, cipher, llm):
    from contextlib import contextmanager

    from fastapi.testclient import TestClient

    from app.api import Deps, create_app
    from app.auth import TokenVerifier
    from app.config import DEFAULT_CLINIC
    from app.ratelimit import RateLimiter

    @contextmanager
    def scope():
        yield repo

    deps = Deps(repo_scope=scope, cipher=cipher, llm=llm, verifier=TokenVerifier(jwt_secret=TEST_JWT_SECRET),
                clinic=DEFAULT_CLINIC, limiter=RateLimiter(), meta_app_secret=TEST_META_SECRET,
                meta_verify_token="verify-me", lead_form_secret="lead-form-secret")
    return TestClient(create_app(deps))


@pytest.fixture
def guest_session(repo):
    return repo.create_lead_session(clinic_id="clinic_demo", source_channel="instagram_ad_click",
                                    identity_level="anonymous", campaign_id="ivf_over40",
                                    guest_auth_user_id="anon-user-1")
