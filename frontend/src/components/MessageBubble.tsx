import { useState } from 'react'
import type { ChatMessage, Citation } from '../lib/api'

/** Turns "...two weeks. [S1] [S2]" into text plus tappable source chips. */
function withCitations(text: string, citations: Citation[], open: string | null, toggle: (m: string) => void) {
  const parts = text.split(/(\s*\[S\d+\])/g)
  return parts.map((part, i) => {
    const match = part.match(/\[(S\d+)\]/)
    if (!match) return <span key={i}>{part}</span>
    const marker = match[1]
    const known = citations.some((c) => c.marker === marker)
    if (!known) return null
    const n = citations.findIndex((c) => c.marker === marker) + 1
    return (
      <button key={i} type="button" className={`cite ${open === marker ? 'cite-open' : ''}`}
              onClick={() => toggle(marker)} aria-expanded={open === marker}
              aria-label={`Source ${n}`}>{n}</button>
    )
  })
}

export function MessageBubble({ message, onSendToClinic, sending }: {
  message: ChatMessage
  onSendToClinic?: () => void
  sending?: boolean
}) {
  const [open, setOpen] = useState<string | null>(null)
  const citations = message.citations ?? []
  const openCitation = citations.find((c) => c.marker === open)
  const label = { assistant: 'Nightingale', clinician: 'Nurse team', system: '', patient: '' }[message.sender]

  return (
    <div className={`row row-${message.sender}`}>
      <div className={`bubble bubble-${message.sender}`}>
        {label && <div className="bubble-label">{label}</div>}
        <div className="bubble-text">
          {withCitations(message.text, citations, open, (m) => setOpen(open === m ? null : m))}
        </div>
        {openCitation && (
          <div className="source">
            <div className="source-title">{openCitation.title} · {openCitation.heading}</div>
            <p>{openCitation.excerpt}</p>
            <div className="source-note">From the clinic's own information</div>
          </div>
        )}
        {citations.length > 0 && !openCitation && (
          <div className="bubble-hint">Tap a number to see where this comes from</div>
        )}
      </div>
      {message.showSendToClinic && onSendToClinic && (
        <button type="button" className="send-to-clinic" onClick={onSendToClinic} disabled={sending}>
          {sending ? 'Sending…' : 'Send to Nurse / Clinic'}
        </button>
      )}
    </div>
  )
}
