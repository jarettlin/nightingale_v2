import { useCallback, useEffect, useState } from 'react'
import { api, ApiError, type QueueItem } from '../lib/api'
import { clearDevToken, devToken } from '../lib/dev'
import { supabase } from '../lib/supabase'
import { EscalationDetail } from './EscalationDetail'
import { PatientsView } from './PatientsView'
import { REASONS, ago, due } from './format'

type Phase = 'loading' | 'signin' | 'check-email' | 'not-staff' | 'ready'

export default function StaffApp() {
  const [phase, setPhase] = useState<Phase>('loading')
  const [tab, setTab] = useState<'queue' | 'patients'>('queue')
  const [queue, setQueue] = useState<QueueItem[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [email, setEmail] = useState('')
  const [error, setError] = useState<string | null>(null)

  const [clinical, setClinical] = useState(false)

  const loadQueue = useCallback(async () => {
    try {
      const [r, me] = await Promise.all([api.queue(), api.staffMe()])
      setQueue(r.escalations)
      setClinical(me.clinical)
      setPhase('ready')
    } catch (err) {
      if (err instanceof ApiError && err.status === 403) setPhase('not-staff')
      else if (err instanceof ApiError && err.status === 401) setPhase('signin')
      else setError('Could not reach the server.')
    }
  }, [])

  useEffect(() => {
    (async () => {
      if (devToken()) return loadQueue()
      const { data } = await supabase.auth.getSession()
      // Patients' anonymous sessions don't count: staff sign in with their own email
      if (data.session && !data.session.user.is_anonymous) return loadQueue()
      setPhase('signin')
    })()
    const { data } = supabase.auth.onAuthStateChange((_e, session) => {
      if (session && !session.user.is_anonymous) loadQueue()
    })
    return () => data.subscription.unsubscribe()
  }, [loadQueue])

  // The queue changes as patients send things: refresh every 30s while open
  useEffect(() => {
    if (phase !== 'ready') return
    const t = setInterval(loadQueue, 30_000)
    return () => clearInterval(t)
  }, [phase, loadQueue])

  async function sendLink(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    // Invite-only: this page never creates accounts (shouldCreateUser: false). Staff
    // accounts are created by an admin with scripts/add_staff.py.
    const { error } = await supabase.auth.signInWithOtp({
      email, options: { emailRedirectTo: `${window.location.origin}/staff`, shouldCreateUser: false },
    })
    // Unknown emails get the same answer as staff ones, so the page can't be used to
    // find out who works here. Only rate limiting is worth telling the person about.
    if (error && error.status === 429) setError('Too many attempts. Please wait a few minutes and try again.')
    else setPhase('check-email')
  }

  async function signOut() {
    clearDevToken()
    await supabase.auth.signOut()
    setQueue([]); setSelected(null); setPhase('signin')
  }

  if (phase === 'loading') return <div className="staff"><p className="muted pad">Loading…</p></div>

  if (phase === 'signin' || phase === 'check-email') {
    return (
      <div className="staff staff-center">
        <div className="card signin">
          <h1>Clinic staff</h1>
          {phase === 'signin' ? (
            <form onSubmit={sendLink}>
              <p className="muted">Sign in with the email your clinic admin invited. We'll send you a link.</p>
              <label className="field">Email
                <input type="email" required autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} />
              </label>
              <button className="primary">Email me a sign-in link</button>
            </form>
          ) : <p>If <strong>{email}</strong> belongs to clinic staff, a sign-in link is on its way. Open it on this
            device. No email? Ask your clinic admin to invite you.</p>}
          {error && <p className="error">{error}</p>}
        </div>
      </div>
    )
  }

  if (phase === 'not-staff') {
    return (
      <div className="staff staff-center">
        <div className="card signin">
          <h1>Not set up yet</h1>
          <p>You're signed in, but this account doesn't have clinic staff access. Ask your clinic admin to
            invite you.</p>
          <button type="button" className="link" onClick={signOut}>Sign out</button>
        </div>
      </div>
    )
  }

  const open = queue.filter((q) => q.status !== 'closed')
  return (
    <div className="staff">
      <header className="staff-top">
        <strong>Nightingale · Clinic</strong>
        <nav>
          <button type="button" className={tab === 'queue' ? 'on' : ''} onClick={() => setTab('queue')}>
            Queue <span className="badge">{open.length}</span>
          </button>
          <button type="button" className={tab === 'patients' ? 'on' : ''} onClick={() => { setTab('patients'); setSelected(null) }}>Patients</button>
        </nav>
        <button type="button" className="link" onClick={signOut}>Sign out</button>
      </header>
      {error && <p className="error pad">{error}</p>}

      {tab === 'queue' ? (
        <div className={`split ${selected ? 'has-detail' : ''}`}>
          <ul className="list" aria-label="Escalation queue">
            {queue.length === 0 && <li className="muted pad">Nothing waiting. New requests appear here automatically.</li>}
            {queue.map((q) => {
              const d = due(q.response_due_at)
              return (
                <li key={q.id}>
                  <button type="button" className={`list-item ${selected === q.id ? 'active' : ''}`} onClick={() => setSelected(q.id)}>
                    <span className="list-row">
                      <span className={`risk risk-${q.risk_level ?? 'low'}`}>{q.risk_level ?? '—'}</span>
                      <strong>{REASONS[q.reason] ?? q.reason}</strong>
                    </span>
                    <span className="list-summary">{q.summary[1] ?? q.summary[0]}</span>
                    <span className="list-row muted">
                      <span className={`pill status-${q.status}`}>{q.status.replace('_', ' ')}</span>
                      <span>{ago(q.created_at)}</span>
                      <span className={d.overdue ? 'overdue' : ''}>{d.text}</span>
                    </span>
                  </button>
                </li>
              )
            })}
          </ul>
          {selected
            ? <EscalationDetail id={selected} clinical={clinical} onChanged={loadQueue} onBack={() => setSelected(null)} />
            : <div className="detail"><p className="muted pad">Choose a request. The most urgent are at the top.</p></div>}
        </div>
      ) : <PatientsView />}
    </div>
  )
}
