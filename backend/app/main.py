"""Production entry point:  .venv/bin/uvicorn app.main:app --reload

Reads backend/.env. Refuses to start without an encryption key (fail closed:
we never store plaintext messages "temporarily"). Without DATABASE_URL it runs
on the in-memory repo so the API can be explored offline.
"""

import logging
import os
from contextlib import contextmanager
from pathlib import Path

from dotenv import load_dotenv

from app.api import Deps, create_app
from app.auth import TokenVerifier
from app.config import DEFAULT_CLINIC, ClinicConfig
from app.crypto import Cipher
from app.llm import OpenAIClient, SafeLLM, UnconfiguredLLM
from app.ratelimit import RateLimiter

load_dotenv(Path(__file__).parent.parent / ".env")
logging.basicConfig(level=logging.INFO, format="%(message)s")


def _supabase_scope(database_url: str):
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    from app.supabase_repo import SupabaseRepo

    pool = ConnectionPool(database_url, min_size=1, max_size=5, open=True,
                          kwargs={"row_factory": dict_row, "autocommit": False})

    @contextmanager
    def scope():
        # The pool commits when the request succeeds and rolls back if it raised
        with pool.connection() as conn:
            yield SupabaseRepo(conn)

    return scope


def _load_clinic(scope, slug: str) -> ClinicConfig:
    with scope() as repo:
        row = repo.conn.execute(
            "select id, name, emergency_number, response_window from public.clinic where slug = %s",
            (slug,)).fetchone()
    if row is None:
        raise RuntimeError(f"clinic {slug!r} not found: run supabase/seed.sql")
    return ClinicConfig(clinic_id=str(row["id"]), name=row["name"], emergency_number=row["emergency_number"],
                        response_window=row["response_window"], description=DEFAULT_CLINIC.description)


def build_app():
    cipher = Cipher.from_env()
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        scope = _supabase_scope(database_url)
        clinic = _load_clinic(scope, os.environ.get("CLINIC_SLUG", "sunrise"))
    else:
        from app.repository import InMemoryRepo
        memory = InMemoryRepo()

        @contextmanager
        def scope():
            yield memory
        clinic = DEFAULT_CLINIC
        logging.warning("DATABASE_URL not set: using in-memory storage (data is lost on restart)")

    if api_key := os.environ.get("OPENAI_API_KEY"):
        model = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")
        llm = SafeLLM(OpenAIClient(api_key, model))
        extractor = llm
        logging.info(f"LLM: OpenAI {model}")
    else:
        llm, extractor = SafeLLM(UnconfiguredLLM()), None
        logging.warning("OPENAI_API_KEY not set: running in safe fallback mode (templates, rules-only risk)")

    verifier = TokenVerifier(supabase_url=os.environ.get("SUPABASE_URL") or None,
                             jwt_secret=os.environ.get("SUPABASE_JWT_SECRET") or None)
    origins = tuple(o.strip() for o in os.environ.get("FRONTEND_ORIGINS", "").split(",") if o.strip())
    return create_app(Deps(repo_scope=scope, cipher=cipher, llm=llm, extractor_llm=extractor,
                           verifier=verifier, clinic=clinic, limiter=RateLimiter(), cors_origins=origins,
                           frontend_url=os.environ.get("FRONTEND_URL", "http://localhost:5173"),
                           meta_app_secret=os.environ.get("META_APP_SECRET", ""),
                           meta_verify_token=os.environ.get("META_VERIFY_TOKEN", ""),
                           lead_form_secret=os.environ.get("LEAD_FORM_SECRET", "")))


app = build_app()
