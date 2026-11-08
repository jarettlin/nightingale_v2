"""Who is calling? Verified server-side on every request.

Supabase Auth issues the JWTs; we only verify them. Three kinds of caller:
  * guest   = Supabase anonymous user (is_anonymous claim). Owns the lead
              sessions created under their user id. Gets value without an account.
  * patient = signed-in user with a patient row. Converting an anonymous user
              keeps the same user id, so their guest sessions stay theirs.
  * staff   = signed-in user with a staff_member row, scoped to one clinic.

Keys: new Supabase projects sign tokens with asymmetric keys published at
<SUPABASE_URL>/auth/v1/.well-known/jwks.json. Older projects (and our tests)
use a shared HS256 secret. Both are supported.
"""

import ssl
from dataclasses import dataclass

import certifi
import jwt

from app.escalation import Staff


class AuthError(Exception):
    """Missing, expired, forged or wrong-audience token: 401."""


class AuthUnavailable(Exception):
    """We couldn't fetch signing keys: 503, and we say so honestly."""


@dataclass
class Principal:
    auth_user_id: str
    is_anonymous: bool
    email: str | None
    role: str                           # guest | patient | staff
    patient_id: str | None = None
    staff: Staff | None = None


class TokenVerifier:
    def __init__(self, supabase_url: str | None = None, jwt_secret: str | None = None):
        self._secret = jwt_secret
        # certifi's CA bundle: some Python installs (python.org on macOS) ship without
        # system root certificates, and key fetching would fail TLS verification
        self._jwks = (jwt.PyJWKClient(f"{supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json",
                                      cache_keys=True, lifespan=3600,
                                      ssl_context=ssl.create_default_context(cafile=certifi.where()))
                      if supabase_url else None)

    def verify(self, token: str) -> dict:
        try:
            alg = jwt.get_unverified_header(token).get("alg")
        except jwt.PyJWTError as exc:
            raise AuthError("malformed token") from exc
        try:
            if alg == "HS256":
                if not self._secret:
                    raise AuthError("HS256 tokens are not accepted")
                key = self._secret
            elif self._jwks is not None:
                key = self._jwks.get_signing_key_from_jwt(token).key
            else:
                raise AuthError("no signing keys configured")
            return jwt.decode(token, key, algorithms=["HS256", "ES256", "RS256"], audience="authenticated",
                              options={"require": ["exp", "sub"]})
        except jwt.PyJWKClientConnectionError as exc:
            raise AuthUnavailable("could not reach Supabase Auth for signing keys") from exc
        except jwt.PyJWTError as exc:
            raise AuthError("invalid token") from exc


def resolve_principal(claims: dict, repo) -> Principal:
    sub = claims["sub"]
    anonymous = bool(claims.get("is_anonymous", False))
    email = None if anonymous else claims.get("email")
    staff = None if anonymous else repo.find_staff_by_auth_user(sub)
    if staff:
        return Principal(sub, False, email, "staff", staff=staff)
    patient_id = None if anonymous else repo.find_patient_by_auth_user(sub)
    return Principal(sub, anonymous, email, "patient" if patient_id else "guest", patient_id=patient_id)
