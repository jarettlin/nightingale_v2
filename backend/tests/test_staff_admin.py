"""Staff invites, without the network: the request Supabase receives, and failures."""

import json

import httpx
import pytest

from app.staff_admin import InviteError, invite_user


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_invite_creates_the_account_and_points_the_link_at_staff():
    seen = {}

    def handler(request: httpx.Request):
        seen.update(url=str(request.url), headers=dict(request.headers), body=json.loads(request.content))
        return httpx.Response(200, json={"id": "new-user-id", "email": "nurse@clinic.example"})

    user_id = invite_user("nurse@clinic.example", "https://proj.supabase.co", "sb_secret_abc",
                          redirect_to="http://localhost:5173/staff", client=client(handler))
    assert user_id == "new-user-id"
    assert seen["url"].startswith("https://proj.supabase.co/auth/v1/invite")
    assert "redirect_to=http%3A%2F%2Flocalhost%3A5173%2Fstaff" in seen["url"]
    assert seen["body"] == {"email": "nurse@clinic.example"}
    assert seen["headers"]["apikey"] == "sb_secret_abc"
    assert "authorization" not in seen["headers"]  # new-style secret keys aren't JWTs


def test_legacy_service_role_key_also_goes_in_authorization():
    seen = {}

    def handler(request):
        seen.update(headers=dict(request.headers))
        return httpx.Response(200, json={"id": "u"})

    invite_user("a@b.example", "https://p.supabase.co", "eyJlegacy", "http://x/staff", client=client(handler))
    assert seen["headers"]["authorization"] == "Bearer eyJlegacy"


@pytest.mark.parametrize("response", [
    httpx.Response(422, json={"msg": "A user with this email address has already been registered"}),
    httpx.Response(401, json={"message": "Invalid API key"}),
    httpx.Response(200, json={}),
])
def test_refusals_are_reported_not_ignored(response):
    with pytest.raises(InviteError):
        invite_user("a@b.example", "https://p.supabase.co", "sb_secret_x", "http://x/staff",
                    client=client(lambda _r: response))
