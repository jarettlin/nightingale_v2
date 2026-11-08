import { useEffect, useState } from 'react'
import { api, type PatientSummary } from '../lib/api'
import { CHANNELS, time } from './format'

type ThreadMessage = Awaited<ReturnType<typeof api.patientThread>>['messages'][number]

/** Consented patients only: the API returns nobody who hasn't agreed to share. */
export function PatientsView() {
  const [patients, setPatients] = useState<PatientSummary[] | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [thread, setThread] = useState<ThreadMessage[] | null>(null)

  useEffect(() => { api.patients().then((r) => setPatients(r.patients)).catch(() => setPatients([])) }, [])
  useEffect(() => {
    if (!selected) return
    setThread(null)
    api.patientThread(selected).then((r) => setThread(r.messages)).catch(() => setThread([]))
  }, [selected])

  if (!patients) return <p className="muted pad">Loading…</p>
  return (
    <div className={`split ${selected ? 'has-detail' : ''}`}>
      <ul className="list">
        {patients.length === 0 && <li className="muted pad">No patients have shared their conversation yet.</li>}
        {patients.map((p) => {
          const concern = p.profile.chief_complaint?.[0]?.value ?? p.profile.goal?.[0]?.value ?? 'No concern recorded'
          return (
            <li key={p.patient_id}>
              <button type="button" className={`list-item ${selected === p.patient_id ? 'active' : ''}`}
                      onClick={() => setSelected(p.patient_id)}>
                <strong>{concern}</strong>
                <span className="muted">{CHANNELS[p.first_touch?.source_channel ?? ''] ?? 'Unknown channel'}
                  {p.first_touch?.landing_timestamp ? ` · first contact ${time(p.first_touch.landing_timestamp)}` : ''}</span>
              </button>
            </li>
          )
        })}
      </ul>
      <div className="detail">
        {selected && <button type="button" className="link back" onClick={() => setSelected(null)}>← Patients</button>}
        {!selected && <p className="muted pad">Choose a patient to read their conversation.</p>}
        {selected && !thread && <p className="muted">Loading…</p>}
        {thread && (
          <div className="staff-thread">
            {thread.map((m) => (
              <div key={m.id} className={`staff-msg from-${m.sender}`}>
                <div className="staff-msg-head">
                  <span>{m.sender === 'patient' ? (m.sent_as === 'guest' ? 'Patient (before sign-up)' : 'Patient') : m.sender}</span>
                  {m.risk_level && <span className={`risk risk-${m.risk_level}`}>{m.risk_level}</span>}
                  <span className="muted">{m.created_at ? time(m.created_at) : ''}</span>
                </div>
                <p>{m.text.replace(/\s*\[S\d+\]/g, '')}</p>
                {m.risk_reason && m.risk_level !== 'low' && <p className="muted small">Risk reason: {m.risk_reason}</p>}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
