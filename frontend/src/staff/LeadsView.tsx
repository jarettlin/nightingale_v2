import { useEffect, useState } from 'react'
import { api, ApiError, type Funnel, type Lead } from '../lib/api'
import { CHANNELS, ago } from './format'

const STEP_LABELS: Record<string, string> = {
  visitor: 'Visited', conversation_started: 'Chatted', value_event: 'Got help', auth_started: 'Started sign-up',
  consented: 'Consented', patient_created: 'Patient', escalation_sent: 'Sent to nurse',
}

/** Staff referral: what the patient asked about at the clinic becomes a personal link. */
function ReferralMaker() {
  const [topic, setTopic] = useState('')
  const [link, setLink] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function make(e: React.FormEvent) {
    e.preventDefault()
    setError(null); setCopied(false)
    try {
      setLink((await api.makeReferral(topic.trim())).link)
      setTopic('')
    } catch (err) {
      setError(err instanceof ApiError ? String(err.detail) : 'Could not create the link.')
    }
  }

  return (
    <section className="card">
      <h3>Referral link</h3>
      <form onSubmit={make} className="referral-form">
        <label className="field">What did they ask about? (no names)
          <input value={topic} maxLength={200} placeholder="asked about egg freezing at today's visit"
                 onChange={(e) => setTopic(e.target.value)} />
        </label>
        <button className="primary" disabled={topic.trim().length < 2}>Create personal link</button>
      </form>
      {link && (
        <div className="link-box">
          <code>{link}</code>
          <button type="button" onClick={async () => {
            try { await navigator.clipboard.writeText(link); setCopied(true) } catch { /* clipboard blocked */ }
          }}>{copied ? 'Copied' : 'Copy'}</button>
          <p className="muted small">Send it by WhatsApp or SMS. It opens a chat that already knows the topic, and works for 7 days.</p>
        </div>
      )}
      {error && <p className="error">{error}</p>}
    </section>
  )
}

function LeadRow({ lead }: { lead: Lead }) {
  const p = lead.score_parts
  return (
    <li className="lead">
      <div className="lead-head">
        <span className="score" title="Score out of 95">{Math.round(lead.score)}</span>
        <strong>{lead.concern ?? 'No topic yet'}</strong>
        {lead.concern_source && <span className="muted small">({lead.concern_source})</span>}
      </div>
      <div className="list-row muted">
        <span>{CHANNELS[lead.channel] ?? lead.channel}</span>
        <span>· {STEP_LABELS[lead.stage] ?? lead.stage}</span>
        <span>· active {ago(lead.last_activity)}</span>
      </div>
      <div className="score-parts small muted">
        recency {p.recency} + stage {p.stage} + reachability {p.identity} + channel {p.channel}
      </div>
      <div className={`suggestion ${lead.priority === 'clinical' ? 'clinical' : ''}`}>{lead.suggestion}</div>
    </li>
  )
}

export function LeadsView() {
  const [funnel, setFunnel] = useState<Funnel | null>(null)
  const [leads, setLeads] = useState<{ leads: Lead[]; clinical: Lead[] } | null>(null)

  useEffect(() => {
    api.funnel().then(setFunnel).catch(() => setFunnel({ steps: [], channels: {} }))
    api.leads().then(setLeads).catch(() => setLeads({ leads: [], clinical: [] }))
  }, [])

  const channels = funnel ? Object.entries(funnel.channels) : []
  return (
    <div className="leads-page">
      <ReferralMaker />

      <section className="card">
        <h3>Funnel by channel · last 30 days</h3>
        {!funnel && <p className="muted">Loading…</p>}
        {funnel && channels.length === 0 && <p className="muted">No visits yet.</p>}
        {channels.length > 0 && (
          <div className="table-wrap">
            <table className="funnel">
              <thead><tr><th>Channel</th>{funnel!.steps.map((s) => <th key={s}>{STEP_LABELS[s]}</th>)}</tr></thead>
              <tbody>
                {channels.map(([ch, row]) => (
                  <tr key={ch}>
                    <th>{CHANNELS[ch] ?? ch}</th>
                    {funnel!.steps.map((s) => (
                      <td key={s}>{row.counts[s]}
                        {s !== 'visitor' && row.rates[s] !== null && <span className="muted"> {Math.round(row.rates[s]! * 100)}%</span>}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="muted small">Each cell counts conversations that reached that step; % is of visitors on that channel.</p>
      </section>

      {leads && leads.clinical.length > 0 && (
        <section className="card">
          <h3>Clinical: handled in the nurse queue</h3>
          <p className="muted small">A clinical concern is a compassion priority, never a sales one.</p>
          <ul className="leads">{leads.clinical.map((l) => <LeadRow key={l.lead_session_id} lead={l} />)}</ul>
        </section>
      )}

      <section className="card">
        <h3>Warm leads</h3>
        <p className="muted small">Ranked by a simple score you can see in full. Health details appear only after the person consents.</p>
        {!leads && <p className="muted">Loading…</p>}
        {leads && leads.leads.length === 0 && <p className="muted">No leads yet.</p>}
        {leads && <ul className="leads">{leads.leads.map((l) => <LeadRow key={l.lead_session_id} lead={l} />)}</ul>}
      </section>
    </div>
  )
}
