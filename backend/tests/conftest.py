import json

import pytest

from app.crypto import ephemeral_cipher
from app.intake import RISK_SYSTEM
from app.llm import SafeLLM
from app.repository import InMemoryRepo


class RoutingLLM:
    """Answers by purpose rather than call order: low risk for risk questions,
    a canned answer otherwise. Records every prompt it receives."""

    def __init__(self, answer: str = "Here is some general information. [S1]"):
        self.answer = answer
        self.prompts: list[dict] = []

    def complete(self, system: str, user: str, timeout_s: float = 10.0) -> str:
        self.prompts.append({"system": system, "user": user})
        if system == RISK_SYSTEM:
            return json.dumps({"risk_level": "low", "reason": "test", "confidence": "high"})
        return self.answer


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


@pytest.fixture
def guest_session(repo):
    return repo.create_lead_session(clinic_id="clinic_demo", source_channel="instagram_ad_click",
                                    identity_level="anonymous", campaign_id="ivf_over40",
                                    guest_auth_user_id="anon-user-1")
