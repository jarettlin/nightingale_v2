"""End-to-end check with real Supabase Auth, against a running API.

    .venv/bin/uvicorn app.main:app --port 8000      # in one terminal
    .venv/bin/python scripts/smoke_auth.py           # in another

Needs SUPABASE_URL and SUPABASE_ANON_KEY in backend/.env, and Anonymous
sign-ins enabled (Authentication → Sign In / Providers). Creates one synthetic
guest session; the daily purge removes it after 7 days.
"""

import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
API = os.environ.get("API_URL", "http://127.0.0.1:8000")


def step(name: str, response: httpx.Response, expect: int):
    ok = response.status_code == expect
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {response.status_code}")
    if not ok:
        print("      ", response.text[:300])
        sys.exit(1)
    return response.json() if response.content else None


def main():
    if not SUPABASE_URL or not ANON_KEY:
        sys.exit("Set SUPABASE_URL and SUPABASE_ANON_KEY in backend/.env first")

    signup = httpx.post(f"{SUPABASE_URL}/auth/v1/signup", json={},
                        headers={"apikey": ANON_KEY, "content-type": "application/json"}, timeout=15)
    data = step("Supabase anonymous sign-in", signup, 200)
    auth = {"Authorization": f"Bearer {data['access_token']}"}
    print("      anonymous user:", data["user"]["is_anonymous"])

    with httpx.Client(base_url=API, timeout=30) as api:
        step("no token is rejected", api.post("/sessions", json={}), 401)
        session = step("guest starts a session", api.post(
            "/sessions", json={"source": "instagram", "campaign": "smoke_test"}, headers=auth), 201)
        sid = session["session"]["id"]
        reply = step("guest sends a message", api.post(
            f"/sessions/{sid}/messages", json={"text": "Are you a real doctor?"}, headers=auth), 200)
        print("      reply:", reply["reply"]["text"][:90], "…")
        thread = step("guest reads their thread", api.get(f"/sessions/{sid}/messages", headers=auth), 200)
        print("      messages stored:", len(thread["messages"]))
        step("guest cannot see the staff queue", api.get("/staff/escalations", headers=auth), 403)
        step("guest cannot send to clinic before signing up", api.post(
            f"/sessions/{sid}/escalations", json={}, headers=auth), 409)
    print("\nall smoke checks passed")


if __name__ == "__main__":
    main()
