import { useEffect, useState } from 'react'
import { api, ApiError, type ClinicInfo, type Profile } from '../lib/api'
import { supabase } from '../lib/supabase'
import { LABELS_SHORT, store } from '../lib/util'

type Step = 'email' | 'check-email' | 'consent'

/**
 * The trust transition. Two steps, and nothing is shared until the patient says so:
 *  1. verify an email (Supabase upgrades the anonymous user in place, same user id,
 *     so everything said as a guest stays theirs)
 *  2. phone + consent: see exactly what will be shared, untick anything, and choose
 *     data sharing and marketing separately
 */
export function ContinueSecurely({ open, sessionId, clinic, profile, isAnonymous, onClose, onConverted }: {
  open: boolean
  sessionId: string
  clinic: ClinicInfo
  profile: Profile
  isAnonymous: boolean
  onClose: () => void
  onConverted: (openingMessage: string, profile: Profile) => void
}) {
  const [step, setStep] = useState<Step>(isAnonymous ? 'email' : 'consent')
  const [email, setEmail] = useState('')
  const [phone, setPhone] = useState('')
  const [share, setShare] = useState(true)
  const [marketing, setMarketing] = useState(false)
  const [excluded, setExcluded] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => { setStep(isAnonymous ? (store.get('pendingEmail') ? 'check-email' : 'email') : 'consent') },
    [isAnonymous, open])
  useEffect(() => { if (open) api.authStarted(sessionId).catch(() => {}) }, [open, sessionId])

  const items = Object.entries(profile).flatMap(([kind, list]) => list.map((i) => ({ ...i, kind })))

  async function sendLink(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true); setError(null)
    // Upgrades the anonymous user: the confirmation link brings them back here signed in
    const { error } = await supabase.auth.updateUser({ email }, { emailRedirectTo: window.location.origin })
    setBusy(false)
    if (error) return setError(error.message)
    store.set('pendingEmail', email)
    setStep('check-email')
  }

  async function finish(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true); setError(null)
    try {
      const result = await api.convert(sessionId, { phone, data_sharing: share, marketing, exclude_memory_ids: excluded })
      store.remove('pendingEmail')
      onConverted(result.opening_message, result.profile)
    } catch (err) {
      setError(err instanceof ApiError ? String(err.detail) : 'Something went wrong. Please try again.')
    } finally {
      setBusy(false)
    }
  }

  if (!open) return null
  return (
    <div className="sheet-backdrop" onClick={onClose}>
      <div className="sheet" role="dialog" aria-modal="true" aria-label="Continue securely" onClick={(e) => e.stopPropagation()}>
        <header className="sheet-head">
          <h2>Continue securely</h2>
          <button type="button" className="icon-button" onClick={onClose} aria-label="Close">✕</button>
        </header>

        {step === 'email' && (
          <form onSubmit={sendLink}>
            <p>So {clinic.name} can reply to you, verify your email. Everything you've said comes with you.</p>
            <label className="field">Email
              <input type="email" required autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} />
            </label>
            <button className="primary" disabled={busy}>{busy ? 'Sending…' : 'Email me a sign-in link'}</button>
          </form>
        )}

        {step === 'check-email' && (
          <div>
            <p>We sent a link to <strong>{store.get('pendingEmail')}</strong>. Open it on this device and you'll come straight back here.</p>
            <button type="button" className="link" onClick={() => { store.remove('pendingEmail'); setStep('email') }}>Use a different email</button>
          </div>
        )}

        {step === 'consent' && (
          <form onSubmit={finish}>
            <label className="field">Phone number (so the nurse can reach you)
              <input type="tel" required autoComplete="tel" placeholder="+60 12 345 6789" value={phone}
                     onChange={(e) => setPhone(e.target.value)} />
            </label>

            {items.length > 0 && (
              <fieldset className="share-list">
                <legend>What {clinic.name} will see</legend>
                {items.map((i) => (
                  <label key={i.id} className="check">
                    <input type="checkbox" checked={!excluded.includes(i.id)}
                           onChange={(e) => setExcluded(e.target.checked ? excluded.filter((x) => x !== i.id) : [...excluded, i.id])} />
                    <span>{LABELS_SHORT[i.kind] ?? i.kind}: {i.value}</span>
                  </label>
                ))}
                <p className="hint">Untick anything you'd rather not carry over. Your full conversation is also shared.</p>
              </fieldset>
            )}

            <label className="check consent">
              <input type="checkbox" checked={share} onChange={(e) => setShare(e.target.checked)} />
              <span>I agree to share my health information from this conversation with <strong>{clinic.name}</strong> so their nurse team can respond.</span>
            </label>
            <label className="check consent">
              <input type="checkbox" checked={marketing} onChange={(e) => setMarketing(e.target.checked)} />
              <span>Optional: send me occasional updates and offers. You can stop these any time.</span>
            </label>

            <button className="primary" disabled={busy}>{busy ? 'Saving…' : 'Continue'}</button>
          </form>
        )}

        {error && <p className="error" role="alert">{error}</p>}
      </div>
    </div>
  )
}
