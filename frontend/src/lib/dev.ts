/**
 * Demo sign-in for local development only (backend/scripts/demo_server.py prints the links).
 *
 * `import.meta.env.DEV` is false in production builds, so Vite removes this code path:
 * a production site never reads ?dev_token= and only ever uses real Supabase sessions.
 */
const KEY = 'nightingale.devToken'

export function devToken(): string | null {
  if (!import.meta.env.DEV) return null
  try {
    const fromUrl = new URLSearchParams(window.location.search).get('dev_token')
    if (fromUrl) {
      window.sessionStorage.setItem(KEY, fromUrl)
      const url = new URL(window.location.href)
      url.searchParams.delete('dev_token')
      window.history.replaceState(null, '', url)   // keep the token out of the address bar
    }
    return window.sessionStorage.getItem(KEY)
  } catch {
    return null
  }
}

export function clearDevToken() {
  try { window.sessionStorage.removeItem(KEY) } catch { /* not available */ }
}
