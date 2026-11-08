import { useEffect, useState } from 'react'
import { api, ApiError, type ReviewPacket, type Source } from '../lib/api'
import { CHANNELS, KINDS, REASONS, due, time } from './format'

/** Quote of the patient's own words behind a bullet or fact. This is what lets a
 *  clinician start a structured review without the patient repeating anything. */
function Quotes({ ids, sources }: { ids: string[]; sources: Record<string, Source> }) {
  const found = ids.map((id) => sources[id]).filter(Boolean)
  if (!found.length) return null
  return (
    <div className="quotes">
      {found.map((s, i) => (
        <blockquote key={i}>
          “{s.text}”
          <cite>{s.sent_as === 'guest' ? 'said before signing up' : 'said as a patient'} · {time(s.created_at)}</cite>
        </blockquote>
      ))}
    </div>
  )
}

export function EscalationDetail({ id, clinical, onChanged, onBack }: {
  id: string
  clinical: boolean   // nurses and clinicians review and reply; front desk acknowledges
  onChanged: () => void
  onBack: () => void
}) {
  const [packet, setPacket] = useState<ReviewPacket | null>(null)
  const [open, setOpen] = useState<string | null>(null)
  const [reply, setReply] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function load() {
    setPacket(await api.review(id))
  }
  useEffect(() => { setPacket(null); setError(null); setReply(''); load().catch(() => setError('Could not load this escalation.')) },
    [id]) // eslint-disable-line react-hooks/exhaustive-deps

  async function act(fn: () => Promise<unknown>) {
    setBusy(true); setError(null)
    try {
      await fn()
      await load()
      onChanged()
      return true
    } catch (err) {
      setError(err instanceof ApiError ? String(err.detail) : 'Something went wrong.')
      return false
    } finally {
      setBusy(false)
    }
  }

  if (error && !packet) return <div className="detail"><p className="error">{error}</p></div>
  if (!packet) return <div className="detail"><p className="muted">Loading…</p></div>

  const e = packet.escalation
  const d = due(e.response_due_at)
  const a = e.attribution
  const facts = e.profile_snapshot.filter((f) => f.status !== 'retracted')

  return (
    <div className="detail">
      <button type="button" className="link back" onClick={onBack}>← Queue</button>
      <header className="detail-head">
        <span className={`risk risk-${e.risk_level ?? 'low'}`}>{e.risk_level ?? '—'}</span>
        <h2>{REASONS[e.reason] ?? e.reason}</h2>
        <div className="detail-meta">
          <span className={`pill status-${e.status}`}>{e.status.replace('_', ' ')}</span>
          <span className={d.overdue ? 'overdue' : 'muted'}>{e.status === 'closed' ? 'closed' : d.text}</span>
          <span className="muted">sent {time(e.created_at)}</span>
        </div>
      </header>

      <section className="card">
        <h3>Triage summary</h3>
        <ul className="triage">
          {e.triage_summary.map((b, i) => (
            <li key={i}>
              <button type="button" className="bullet" onClick={() => setOpen(open === `b${i}` ? null : `b${i}`)}
                      disabled={!b.provenance_message_ids.length} aria-expanded={open === `b${i}`}>
                {b.text}
                {b.provenance_message_ids.length > 0 && <span className="src-link">{open === `b${i}` ? 'hide' : 'source'}</span>}
              </button>
              {open === `b${i}` && <Quotes ids={b.provenance_message_ids} sources={packet.sources} />}
            </li>
          ))}
        </ul>
      </section>

      <section className="card">
        <h3>Profile when they pressed Send</h3>
        {facts.length === 0 && <p className="muted">No structured facts yet.</p>}
        <ul className="facts">
          {facts.map((f) => {
            const when = f.detail.onset || f.detail.since || f.detail.effective
            return (
              <li key={f.id}>
                <button type="button" className="bullet" onClick={() => setOpen(open === f.id ? null : f.id)}>
                  <span className="muted">{KINDS[f.kind] ?? f.kind}:</span> <strong>{f.value}</strong>{' '}
                  <span className="pill">{f.status}</span>{when && <span className="muted"> · {when}</span>}
                  {f.versions > 1 && <span className="muted"> · updated {f.versions - 1}×</span>}
                  {f.conflict_with_id && <span className="overdue"> · ⚠ conflicting answer</span>}
                  <span className="src-link">{open === f.id ? 'hide' : 'source'}</span>
                </button>
                {open === f.id && <Quotes ids={[f.provenance_message_id]} sources={packet.sources} />}
              </li>
            )
          })}
        </ul>
      </section>

      <section className="card">
        <h3>How they found the clinic</h3>
        <dl className="attribution">
          <dt>Channel</dt><dd>{CHANNELS[a.source_channel ?? ''] ?? a.source_channel}</dd>
          {a.campaign_id && <><dt>Campaign</dt><dd>{a.campaign_id}{a.creative ? ` · ${a.creative}` : ''}</dd></>}
          {a.referral_topic && <><dt>Referral note</dt><dd>{a.referral_topic}</dd></>}
          {a.landing_timestamp && <><dt>First contact</dt><dd>{time(a.landing_timestamp)}</dd></>}
        </dl>
      </section>

      {packet.responses.length > 0 && (
        <section className="card">
          <h3>Replies sent</h3>
          {packet.responses.map((r, i) => (
            <div key={i} className="sent-reply"><p>{r.text}</p><span className="muted">{time(r.created_at)}</span></div>
          ))}
        </section>
      )}

      {e.status !== 'closed' && (
        <section className="card actions-card">
          <div className="status-actions">
            {e.status === 'sent' && <button type="button" disabled={busy} onClick={() => act(() => api.setStatus(id, 'acknowledged'))}>Acknowledge</button>}
            {clinical && ['sent', 'acknowledged', 'responded'].includes(e.status) &&
              <button type="button" disabled={busy} onClick={() => act(() => api.setStatus(id, 'in_review'))}>Start review</button>}
            <button type="button" disabled={busy} onClick={() => act(() => api.setStatus(id, 'closed'))}>Close</button>
          </div>
          {clinical ? (
            <form onSubmit={async (ev) => {
              ev.preventDefault()
              if (reply.trim() && await act(() => api.reply(id, reply.trim()))) setReply('')
            }}>
              <label className="field">Reply to the patient (appears in their chat)
                <textarea rows={3} value={reply} onChange={(ev) => setReply(ev.target.value)} />
              </label>
              <button className="primary" disabled={busy || !reply.trim()}>Send reply</button>
            </form>
          ) : <p className="muted small">Only nurses and clinicians can review and reply. Acknowledge it so the team knows it's been seen.</p>}
        </section>
      )}
      {error && <p className="error" role="alert">{error}</p>}
    </div>
  )
}
