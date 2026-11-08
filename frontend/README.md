# Nightingale PWA

Patient messenger for the Nightingale API: React + TypeScript + Vite, installable as a PWA.

## Run

1. Start the API (from `backend/`): `.venv/bin/uvicorn app.main:app --port 8000`
2. Copy `.env.example` to `.env.local` and fill in the Supabase URL and **publishable** key.
3. `npm install`, then `npm run dev` and open http://localhost:5173/?source=instagram&campaign=ivf_over40

In development, `/api/*` is proxied to `127.0.0.1:8000`, so there is no CORS setup.

## What's here

- `src/App.tsx`: the thread. Guests are signed in anonymously and get answers before any sign-up. The last
  conversation is resumed on reload, and `?source=` / `?campaign=` attribute new sessions.
- `src/components/MessageBubble.tsx`: replies with tappable source numbers that show the clinic's own text.
- `src/components/ProfileDrawer.tsx`: "What you've shared", the live profile. Tap a fact to jump to the message it came from.
- `src/components/ContinueSecurely.tsx`: email verification (upgrades the anonymous user in place), then phone,
  a list of what will be shared with per-item opt-out, and separate data-sharing and marketing consent.

The service worker caches the app shell only. Conversations are never stored offline.
