import { useEffect, useState } from 'react'
import { api, ApiError } from '../lib/api'

type Note = Awaited<ReturnType<typeof api.shareNote>>

/** Value before sign-up: a note they can share, or a summary emailed to them. */
export function ValueSheet({ kind, sessionId, onClose }: {
  kind: 'note' | 'email' | null
  sessionId: string
  onClose: () => void
}) {
  const [note, setNote] = useState<Note | null>(null)
  const [email, setEmail] = useState('')
  const [marketing, setMarketing] = useState(false)
  const [done, setDone] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    setNote(null); setDone(null); setError(null)
    if (kind === 'note') {
      api.shareNote(sessionId).then(setNote).catch((err) =>
        setError(err instanceof ApiError ? String(err.detail) : 'Could not write a note right now.'))
    }
  }, [kind, sessionId])

  if (!kind) return null

  async function share() {
    if (!note) return
    try {
      if (navigator.share) await navigator.share({ text: note.text })
      else { await navigator.clipboard.writeText(note.text); setDone('Copied. Paste it anywhere.') }
    } catch { /* share sheet dismissed */ }
  }

  async function sendEmail(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true); setError(null)
    try {
      await api.emailSummary(sessionId, email.trim(), marketing)
      setDone(`Sent to ${email.trim()}. Personal details you shared are left out of the email.`)
    } catch (err) {
      setError(err instanceof ApiError && err.status === 429 ? 'Please wait a little before sending another.'
        : err instanceof ApiError ? String(err.detail) : 'Could not send right now.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="sheet-backdrop" onClick={onClose}>
      <div className="sheet" role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()}>
        <header className="sheet-head">
          <h2>{kind === 'note' ? 'A note to share' : 'Email me a summary'}</h2>
          <button type="button" className="icon-button" onClick={onClose} aria-label="Close">✕</button>
        </header>

        {kind === 'note' && (
          <>
            <p className="hint">Something you can send a friend or post. It doesn't mention the clinic or anything you told me.</p>
            {!note && !error && <p className="hint">Writing…</p>}
            {note && (
              <>
                <blockquote className="note">{note.text}</blockquote>
                <p className="hint">{note.chars}/240 characters · fact from “{note.source.heading}”</p>
                <button type="button" className="primary" onClick={share}>Share or copy</button>
              </>
            )}
          </>
        )}

        {kind === 'email' && !done && (
          <form onSubmit={sendEmail}>
            <p className="hint">A short summary plus 6 questions people often forget to ask their doctor.
              Personal details stay here in the chat, not in the email.</p>
            <label className="field">Email
              <input type="email" required autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} />
            </label>
            <label className="check">
              <input type="checkbox" checked={marketing} onChange={(e) => setMarketing(e.target.checked)} />
              <span>Optional: also send me occasional updates. Leave unticked for just this one email.</span>
            </label>
            <button className="primary" disabled={busy}>{busy ? 'Sending…' : 'Send it'}</button>
          </form>
        )}

        {done && <p>{done}</p>}
        {error && <p className="error" role="alert">{error}</p>}
      </div>
    </div>
  )
}
