"""Chat with the running API as a real Supabase anonymous guest.

    .venv/bin/uvicorn app.main:app --port 8000          # terminal 1
    .venv/bin/python scripts/chat.py                     # terminal 2, type messages
    .venv/bin/python scripts/chat.py "msg one" "msg two" # or send a fixed script

Shows each reply with its risk level, the Send to Clinic button, the sign-up
offer, and the live profile, which is what the frontend will render.
"""

import os
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
API = os.environ.get("API_URL", "http://127.0.0.1:8000")


def sign_in_anonymously() -> dict:
    url, key = os.environ["SUPABASE_URL"].rstrip("/"), os.environ["SUPABASE_ANON_KEY"]
    r = httpx.post(f"{url}/auth/v1/signup", json={}, headers={"apikey": key}, timeout=15)
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def show(body: dict) -> None:
    print(f"\nnightingale [{body['risk_level']}]: {body['reply']['text']}")
    if body["show_send_to_clinic"]:
        print("  [ Send to Nurse/Clinic ]")
    if body["conversion_offer"]:
        print(f"  offer: {body['conversion_offer']}")
    facts = [f"{kind}: {i['value']} ({i['status']})" for kind, items in body["profile"].items() for i in items]
    if facts:
        print("  profile: " + " | ".join(facts))


def main(messages: list[str]) -> None:
    auth = sign_in_anonymously()
    with httpx.Client(base_url=API, timeout=60, headers=auth) as api:
        start = api.post("/sessions", json={"source": "instagram", "campaign": "ivf_over40"}).json()
        print(start["greeting"])
        print(f"({start['emergency_notice']})")
        source = messages or (line.strip() for line in sys.stdin)
        for text in source:
            if not text:
                continue
            print(f"\nyou: {text}")
            started = time.perf_counter()
            r = api.post(f"/sessions/{start['session']['id']}/messages", json={"text": text})
            elapsed = time.perf_counter() - started
            if r.status_code != 200:
                print(f"  error {r.status_code}: {r.text[:200]}")
                continue
            show(r.json())
            print(f"  ({elapsed:.1f}s)")


if __name__ == "__main__":
    main(sys.argv[1:])
