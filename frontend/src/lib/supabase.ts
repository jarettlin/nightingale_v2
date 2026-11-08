import { createClient } from '@supabase/supabase-js'

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
  pending ??= signIn().finally(() => { pending = null })
  return pending
}
