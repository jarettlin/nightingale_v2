"""Local demo server: in-memory, pre-filled with synthetic escalations.

    .venv/bin/python scripts/demo_server.py          # serves on 127.0.0.1:8000
    (stop the real server first; the frontend's /api proxy points at port 8000)

Prints sign-in links for a nurse, a front-desk user and a demo patient. They
work only with the frontend's development build (`npm run dev`): the dev build
accepts a ?dev_token= link, the production build ignores it entirely.

Nothing touches Supabase or OpenAI. Tokens are signed with a random secret that
exists only while this process runs.
"""

import secrets
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import jwt
import uvicorn

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.api import Deps, create_app  # noqa: E402
from app.auth import TokenVerifier  # noqa: E402
from app.config import DEFAULT_CLINIC  # noqa: E402
from app.crypto import ephemeral_cipher  # noqa: E402
from app.demo import ScriptedLLM, seed  # noqa: E402
from app.llm import SafeLLM  # noqa: E402
from app.ratelimit import RateLimiter  # noqa: E402
from app.repository import InMemoryRepo  # noqa: E402

FRONTEND = "http://localhost:5173"


def token(secret: str, sub: str, email: str | None, anonymous: bool = False) -> str:
    claims = {"sub": sub, "aud": "authenticated", "role": "authenticated", "is_anonymous": anonymous,
              "exp": int(time.time()) + 12 * 3600}
    if email:
        claims["email"] = email
    return jwt.encode(claims, secret, algorithm="HS256")


def main():
    secret = secrets.token_urlsafe(32)
    repo, cipher = InMemoryRepo(), ephemeral_cipher()
    seed(repo, cipher)

    @contextmanager
    def scope():
        yield repo

    app = create_app(Deps(repo_scope=scope, cipher=cipher, llm=SafeLLM(ScriptedLLM()),
                          verifier=TokenVerifier(jwt_secret=secret), clinic=DEFAULT_CLINIC, limiter=RateLimiter()))

    print("\nDemo data: 3 synthetic patients, 3 escalations (high, med, diagnosis question)\n")
    print("Nurse console:      ", f"{FRONTEND}/staff?dev_token={token(secret, 'demo-nurse', 'nurse@demo.example')}")
    print("Front-desk console: ", f"{FRONTEND}/staff?dev_token={token(secret, 'demo-front-desk', 'desk@demo.example')}")
    print("Patient (Mei):      ", f"{FRONTEND}/?dev_token={token(secret, 'demo-patient-mei', 'mei.demo@example.com')}")
    print()
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")


if __name__ == "__main__":
    main()
