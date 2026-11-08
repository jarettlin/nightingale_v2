import { createClient } from '@supabase/supabase-js'
import { devToken } from './dev'

// Publishable key only: everything it can reach is limited by RLS.
export const supabase = createClient(
  import.meta.env.VITE_SUPABASE_URL as string,
  import.meta.env.VITE_SUPABASE_ANON_KEY as string,
  { auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: true } },
)

let pending: ReturnType<typeof signIn> | null = null

async function signIn() {
  const { data } = await supabase.auth.getSession()
  if (data.session) return data.session
  const { data: anon, error } = await supabase.auth.signInAnonymously()
  if (error || !anon.session) throw new Error(error?.message ?? 'Could not start a session')
  return anon.session
}

/** A signed-in user, anonymous if needed: guests get value before any sign-up.
 *  Shared promise, so two callers at once can't create two anonymous users. */
export function ensureUser() {
  // Dev-build demo sign-in: no Supabase session needed (see lib/dev.ts). Read whether the demo
  // user is anonymous from the token itself, or a new visitor looks signed-in mid sign-up.
  const dev = devToken()
  if (dev) {
    let anonymous = false
    try { anonymous = Boolean(JSON.parse(atob(dev.split('.')[1].replace(/-/g, '+').replace(/_/g, '/'))).is_anonymous) } catch { /* malformed */ }
    return Promise.resolve({ user: { is_anonymous: anonymous } } as Awaited<ReturnType<typeof signIn>>)
  }
  pending ??= signIn().finally(() => { pending = null })
  return pending
}
