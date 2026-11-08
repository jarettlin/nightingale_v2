"""Staff accounts are created internally, never by self sign-up.

The /staff page only signs in accounts that already exist. An admin creates
them here: Supabase's admin API invites the person by email (that creates
the account, and the invite email is their first sign-in link), and the role
goes in our own staff_member table. Access is checked on every request, so
revoking a role takes effect immediately.

The admin API needs the project's secret key (SUPABASE_SECRET_KEY). It bypasses
all security rules, so it lives only in backend/.env on the admin's machine or
server: never in the frontend, never in git.
"""

import httpx


class InviteError(Exception):
    pass


def invite_user(email: str, supabase_url: str, secret_key: str, redirect_to: str,
                client: httpx.Client | None = None) -> str:
    """Create the account and send the invite email. Returns the new user id."""
    headers = {"apikey": secret_key, "content-type": "application/json"}
    if secret_key.startswith("eyJ"):  # legacy service_role keys are JWTs and also go in Authorization
        headers["authorization"] = f"Bearer {secret_key}"
    http = client or httpx.Client(timeout=15)
    try:
        response = http.post(f"{supabase_url.rstrip('/')}/auth/v1/invite", params={"redirect_to": redirect_to},
                             json={"email": email}, headers=headers)
    except httpx.HTTPError as exc:
        raise InviteError(f"could not reach Supabase: {type(exc).__name__}") from exc
    if response.status_code >= 400:
        detail = response.json().get("msg") or response.json().get("message") if response.content else ""
        raise InviteError(f"Supabase refused the invite ({response.status_code}): {detail}")
    user_id = response.json().get("id")
    if not user_id:
        raise InviteError("Supabase did not return a user id")
    return user_id
