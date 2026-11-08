import type { Profile } from '../lib/api'

const LABELS: Record<string, string> = {
  chief_complaint: 'Main concern',
  goal: 'Looking into',
  symptom: 'Symptoms',
  medication: 'Medications',
  allergy: 'Allergies',
  condition: 'Conditions',
  procedure: 'Procedures',
  other: 'Other',
}

export function profileCount(profile: Profile) {
  return Object.values(profile).reduce((n, items) => n + items.length, 0)
}

export function ProfileDrawer({ profile, open, onClose, onJumpTo }: {
  profile: Profile
  open: boolean
  onClose: () => void
  onJumpTo: (messageId: string) => void
}) {
  const kinds = Object.keys(LABELS).filter((k) => profile[k]?.length)
  return (
    <div className={`drawer-backdrop ${open ? 'open' : ''}`} onClick={onClose} aria-hidden={!open}>
      <aside className="drawer" onClick={(e) => e.stopPropagation()} aria-label="What I've shared">
        <header className="drawer-head">
          <h2>What you've shared</h2>
          <button type="button" className="icon-button" onClick={onClose} aria-label="Close">✕</button>
        </header>
        <p className="drawer-intro">
          Nightingale keeps track so you never have to repeat yourself. Tap an item to see where you said it.
        </p>
        {kinds.length === 0 && <p className="drawer-empty">Nothing yet. It fills in as you chat.</p>}
        {kinds.map((kind) => (
          <section key={kind} className="drawer-section">
            <h3>{LABELS[kind]}</h3>
            {profile[kind].map((item) => {
              const when = item.detail.onset || item.detail.since || item.detail.effective
              return (
                <button key={item.id} type="button" className="fact" onClick={() => onJumpTo(item.provenance_message_id)}>
                  <span className="fact-value">{item.value}</span>
                  <span className={`status status-${item.status}`}>{item.status}</span>
                  {when && <span className="fact-when">{when}</span>}
                  {item.conflict_with_id && <span className="fact-conflict">⚠ Conflicts with another answer, a nurse will check</span>}
                  {item.versions > 1 && <span className="fact-history">Updated {item.versions - 1}×</span>}
                </button>
              )
            })}
          </section>
        ))}
      </aside>
    </div>
  )
}
