import { useCallback, useEffect, useRef, useState } from 'react'
import { ContinueSecurely } from './components/ContinueSecurely'
import { MessageBubble } from './components/MessageBubble'
import { ProfileDrawer, profileCount } from './components/ProfileDrawer'
import { api, ApiError, type ChatMessage, type ClinicInfo, type Profile } from './lib/api'
import { ensureUser, supabase } from './lib/supabase'
import { store } from './lib/util'

function arrivalParams() {
  const q = new URLSearchParams(window.location.search)
  return {
    source: q.get('source') ?? 'direct',
    campaign: q.get('campaign') ?? undefined,
    creative: q.get('creative') ?? undefined,
    landing_page: document.referrer || undefined,
  }
}

export default function App() {
  const [clinic, setClinic] = useState<ClinicInfo | null>(null)
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [profile, setProfile] = useState<Profile>({})
  const [offer, setOffer] = useState<string | null>(null)
  const [offerDismissed, setOfferDismissed] = useState(false)
  const [isAnonymous, setIsAnonymous] = useState(true)
  const [converted, setConverted] = useState(false)
  const [highRisk, setHighRisk] = useState(false)
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [escalating, setEscalating] = useState(false)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [sheetOpen, setSheetOpen] = useState(false)
  const [fatal, setFatal] = useState<string | null>(null)
  const endRef = useRef<HTMLDivElement>(null)

  const startNewSession = useCallback(async () => {
    const created = await api.createSession(arrivalParams())
    store.set('session', created.session.id)
    setSessionId(created.session.id)
    setMessages([])
  }, [])

  // Boot: sign in (anonymously if needed), then resume the last conversation or start one.
  // Guarded so React's dev double-run can't create two sessions.
  const booted = useRef(false)
  useEffect(() => {
    if (booted.current) return
    booted.current = true;
    (async () => {
      try {
        const session = await ensureUser()
        setIsAnonymous(session.user.is_anonymous ?? false)
        setClinic(await api.clinic())
        // Resume by asking the server what this user owns, not by what this browser
        // remembers: the email link often opens in another browser, and must land on the
        // same conversation. The id saved on this device only decides which one, if several.
        const mine = (await api.mySessions()).sessions
        const saved = store.get('session')
        const ordered = [...mine.filter((s) => s.id === saved), ...mine.filter((s) => s.id !== saved)]
        for (const candidate of ordered) {
          try {
            const [thread, prof] = await Promise.all([api.messages(candidate.id), api.profile(candidate.id)])
            store.set('session', candidate.id)
            setSessionId(candidate.id)
            setMessages(thread.messages)
            setProfile(prof.profile)
            setConverted(candidate.converted)
            // Signed in (email verified) but not yet a patient: they're mid sign-up, so
            // go straight to the consent step, in whichever browser the link opened
            if (!session.user.is_anonymous && !candidate.converted) setSheetOpen(true)
            return
          } catch (err) {
            if (!(err instanceof ApiError) || ![404, 410].includes(err.status)) throw err
          }
        }
        await startNewSession()
      } catch (err) {
        setFatal(err instanceof Error ? err.message : 'Could not connect')
      }
    })()
  }, [startNewSession])

  useEffect(() => {
    const { data } = supabase.auth.onAuthStateChange((_event, session) => {
      setIsAnonymous(session?.user.is_anonymous ?? true)
    })
    return () => data.subscription.unsubscribe()
  }, [])

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' }) }, [messages, busy])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { setSheetOpen(false); setDrawerOpen(false) }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  async function send(e?: React.FormEvent) {
    e?.preventDefault()
    const text = draft.trim()
    if (!text || !sessionId || busy) return
    setDraft('')
    setBusy(true)
    const tempId = `local-${Date.now()}`
    setMessages((m) => [...m, { id: tempId, sender: 'patient', text }])
    try {
      const r = await api.send(sessionId, text)
      setMessages((m) => [
        ...m.map((x) => (x.id === tempId ? { ...x, id: r.message_id } : x)),
        { id: r.reply.id, sender: 'assistant', text: r.reply.text, citations: r.reply.citations,
          showSendToClinic: r.show_send_to_clinic, riskLevel: r.risk_level },
      ])
      setProfile(r.profile)
      setHighRisk((h) => h || r.risk_level === 'high')
      if (r.conversion_offer) setOffer(r.conversion_offer)
    } catch (err) {
      const msg = err instanceof ApiError && err.status === 429
        ? 'You\'re sending messages quickly. Please wait a moment.'
        : 'Sorry, that didn\'t send. Please try again.'
      setMessages((m) => [...m.filter((x) => x.id !== tempId), { id: `err-${Date.now()}`, sender: 'system', text: msg }])
      setDraft(text)
    } finally {
      setBusy(false)
    }
  }

  async function sendToClinic() {
    if (!sessionId) return
    setEscalating(true)
    try {
      const r = await api.escalate(sessionId)
      setMessages((m) => [...m.map((x) => ({ ...x, showSendToClinic: false })),
        { id: `esc-${r.escalation_id}-${Date.now()}`, sender: 'system', text: r.confirmation }])
    } catch (err) {
      const code = err instanceof ApiError ? (err.detail as { code?: string })?.code : undefined
      if (code === 'needs_account' || code === 'needs_consent') setSheetOpen(true)
      else setMessages((m) => [...m, { id: `err-${Date.now()}`, sender: 'system', text: 'Sorry, that didn\'t send. Please try again.' }])
    } finally {
      setEscalating(false)
    }
  }

  function onConverted(opening: string, prof: Profile) {
    setSheetOpen(false)
    setConverted(true)
    setOffer(null)
    setProfile(prof)
    setMessages((m) => [...m, { id: `welcome-${Date.now()}`, sender: 'assistant', text: opening }])
  }

  function jumpTo(messageId: string) {
    setDrawerOpen(false)
    const el = document.getElementById(`m-${messageId}`)
    el?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    el?.classList.add('flash')
    setTimeout(() => el?.classList.remove('flash'), 1600)
  }

  if (fatal) {
    return <div className="fatal"><h1>Nightingale</h1><p>We couldn't connect right now. If this is an emergency, call 999.</p><p className="hint">{fatal}</p></div>
  }

  const lastAssistant = [...messages].reverse().find((m) => m.sender === 'assistant')
  const facts = profileCount(profile)
  // Always first, for new and resumed conversations alike (it isn't stored server-side)
  const greeting: ChatMessage = {
    id: 'greeting', sender: 'assistant',
    text: `Hi, I'm Nightingale, ${clinic?.name ?? 'the clinic'}'s assistant. Ask me anything about our services, `
      + 'hours or what to expect. No sign-up needed.',
  }

  return (
    <div className="app">
      <header className="topbar">
        <div className="avatar" aria-hidden>N</div>
        <div className="topbar-text">
          <div className="topbar-title">{clinic?.name ?? 'Nightingale'}</div>
          <div className="topbar-sub">Nightingale AI assistant · nurses reply in {clinic?.response_window ?? '12 to 18 hours'}</div>
        </div>
        <button type="button" className="profile-button" onClick={() => setDrawerOpen(true)}>
          Shared <span className="badge">{facts}</span>
        </button>
      </header>

      {highRisk && clinic && (
        <a className="emergency-banner" href={`tel:${clinic.emergency_number}`}>
          If this is an emergency, call {clinic.emergency_number} now →
        </a>
      )}

      <main className="thread" aria-live="polite">
        {clinic && <MessageBubble message={greeting} />}
        {messages.map((m) => (
          <div key={m.id} id={`m-${m.id}`}>
            <MessageBubble message={m} sending={escalating}
                           onSendToClinic={m === lastAssistant ? sendToClinic : undefined} />
          </div>
        ))}
        {busy && <div className="row row-assistant"><div className="bubble bubble-assistant typing"><span /><span /><span /></div></div>}
        <div ref={endRef} />
      </main>

      {offer && !offerDismissed && !converted && (
        <div className="offer">
          <p>{offer}</p>
          <div className="offer-actions">
            <button type="button" className="primary small" onClick={() => setSheetOpen(true)}>Continue securely</button>
            <button type="button" className="link" onClick={() => setOfferDismissed(true)}>Not now</button>
          </div>
        </div>
      )}

      <form className="composer" onSubmit={send}>
        <textarea value={draft} rows={1} placeholder="Ask anything…" aria-label="Message"
                  onChange={(e) => setDraft(e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() } }} />
        <button className="send" disabled={!draft.trim() || busy || !sessionId} aria-label="Send">➤</button>
      </form>
      <p className="emergency-note">{clinic?.emergency_notice ?? 'If this is an emergency, exit Nightingale and dial 999 for Emergency Services.'}</p>

      <ProfileDrawer profile={profile} open={drawerOpen} onClose={() => setDrawerOpen(false)} onJumpTo={jumpTo} />
      {clinic && sessionId && (
        <ContinueSecurely open={sheetOpen} sessionId={sessionId} clinic={clinic} profile={profile}
                          isAnonymous={isAnonymous} onClose={() => setSheetOpen(false)} onConverted={onConverted} />
      )}
    </div>
  )
}
