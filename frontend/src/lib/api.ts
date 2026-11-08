import { devToken } from './dev'
import { supabase } from './supabase'

const BASE = (import.meta.env.VITE_API_URL as string) || '/api'

export type Citation = {
  marker: string
  title: string
  heading: string
  source_doc: string
  excerpt: string
}

export type ProfileItem = {
  id: string
  value: string
  status: string
  detail: Record<string, string>
  provenance_message_id: string
  updated_at: string
  conflict_with_id: string | null
  versions: number
}
export type Profile = Record<string, ProfileItem[]>

export type ChatMessage = {
  id: string
  sender: 'patient' | 'assistant' | 'clinician' | 'system'
  text: string
  created_at?: string
  citations?: Citation[]
  showSendToClinic?: boolean
  riskLevel?: string
}

export type ClinicInfo = {
  name: string
  description: string
  emergency_number: string
  response_window: string
  emergency_notice: string
}

export type MessageResponse = {
  message_id: string
  reply: { id: string; text: string; citations: Citation[]; confidence: string }
  risk_level: 'low' | 'med' | 'high'
  show_send_to_clinic: boolean
  conversion_offer: string | null
  profile: Profile
  emergency_notice: string
}

export class ApiError extends Error {
  status: number
  detail: unknown
  constructor(status: number, detail: unknown) {
    super(typeof detail === 'string' ? detail : `Request failed (${status})`)
    this.status = status
    this.detail = detail
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { 'content-type': 'application/json' }
  const dev = devToken()
  if (dev) {
    headers.authorization = `Bearer ${dev}`
  } else {
    const { data } = await supabase.auth.getSession()
    if (data.session) headers.authorization = `Bearer ${data.session.access_token}`
  }
  const res = await fetch(`${BASE}${path}`, { method, headers, body: body ? JSON.stringify(body) : undefined })
  if (res.status === 204) return undefined as T
  const json = await res.json().catch(() => ({}))
  if (!res.ok) throw new ApiError(res.status, json.detail ?? json)
  return json as T
}

export const api = {
  clinic: () => request<ClinicInfo>('GET', '/clinic'),
  createSession: (params: { source?: string; campaign?: string; creative?: string; landing_page?: string }) =>
    request<{ session: { id: string; source_channel: string }; greeting: { id: string; text: string };
      stat: { text: string; kind: 'count' | 'alternative'; value: number | null };
      skip: string[]; emergency_notice: string }>('POST', '/sessions', params),
  shareNote: (sid: string) =>
    request<{ text: string; chars: number; topic: string; source: { title: string; heading: string; excerpt: string } }>(
      'POST', `/sessions/${sid}/share-note`),
  emailSummary: (sid: string, email: string, marketing_consent: boolean) =>
    request<{ sent: boolean; subject: string }>('POST', `/sessions/${sid}/email-summary`, { email, marketing_consent }),
  claim: (token: string) =>
    request<{ session: { id: string; source_channel: string }; skip: string[] }>('POST', '/sessions/claim', { token }),
  messages: (sid: string) => request<{ messages: ChatMessage[] }>('GET', `/sessions/${sid}/messages`),
  mySessions: () =>
    request<{ sessions: { id: string; source_channel: string; converted: boolean }[] }>('GET', '/me/sessions'),
  profile: (sid: string) => request<{ profile: Profile }>('GET', `/sessions/${sid}/profile`),
  send: (sid: string, text: string) => request<MessageResponse>('POST', `/sessions/${sid}/messages`, { text }),
  authStarted: (sid: string) => request<void>('POST', `/sessions/${sid}/auth-started`),
  convert: (sid: string, body: { phone: string; data_sharing: boolean; marketing: boolean; exclude_memory_ids: string[] }) =>
    request<{ patient_id: string; opening_message: string; profile: Profile }>('POST', `/sessions/${sid}/convert`, body),
  updateConsent: (body: { data_sharing?: boolean; marketing?: boolean }) =>
    request<{ data_sharing: boolean; marketing: boolean }>('POST', '/me/consent', body),
  escalate: (sid: string) =>
    request<{ escalation_id: string; confirmation: string; already_open: boolean; response_due_at: string }>(
      'POST', `/sessions/${sid}/escalations`, {}),

  // ---- staff
  staffMe: () => request<{ role: string; clinical: boolean }>('GET', '/staff/me'),
  makeReferral: (topic: string) =>
    request<{ link: string; topic: string; expires_at: string }>('POST', '/staff/referrals', { topic }),
  funnel: (days = 30) => request<Funnel>('GET', `/staff/funnel?days=${days}`),
  leads: () => request<{ leads: Lead[]; clinical: Lead[] }>('GET', '/staff/leads'),
  queue: () => request<{ escalations: QueueItem[] }>('GET', '/staff/escalations'),
  review: (id: string) => request<ReviewPacket>('GET', `/staff/escalations/${id}`),
  setStatus: (id: string, status: string) =>
    request<{ id: string; status: string }>('POST', `/staff/escalations/${id}/status`, { status }),
  reply: (id: string, body: string) =>
    request<{ id: string }>('POST', `/staff/escalations/${id}/responses`, { body, visible_to_patient: true }),
  patients: () => request<{ patients: PatientSummary[] }>('GET', '/staff/patients'),
  patientThread: (pid: string) =>
    request<{ messages: (ChatMessage & { risk_level?: string; risk_reason?: string; sent_as: string })[] }>(
      'GET', `/staff/patients/${pid}/messages`),
}

export type QueueItem = {
  id: string
  risk_level: 'low' | 'med' | 'high' | null
  reason: string
  status: string
  created_at: string
  response_due_at: string
  summary: string[]
}

export type TriageBullet = { text: string; provenance_message_ids: string[] }
export type SnapshotItem = ProfileItem & { kind: string }
export type Source = { text: string; sent_as: string; created_at: string; risk_level: string | null }

export type ReviewPacket = {
  escalation: {
    id: string
    reason: string
    risk_level: string | null
    status: string
    trigger_message_id: string
    triage_summary: TriageBullet[]
    profile_snapshot: SnapshotItem[]
    attribution: Record<string, string | null>
    response_due_at: string
    created_at: string
    assigned_to: string | null
  }
  sources: Record<string, Source>
  responses: { author_id: string; created_at: string; text: string }[]
}

export type Funnel = {
  steps: string[]
  channels: Record<string, { counts: Record<string, number>; rates: Record<string, number | null> }>
}

export type Lead = {
  lead_session_id: string
  channel: string
  identity_level: string
  stage: string
  last_activity: string
  score: number
  score_parts: { recency: number; stage: number; identity: number; channel: number }
  concern: string | null
  concern_source: string | null
  converted: boolean
  suggestion: string
  priority?: string
}

export type PatientSummary = {
  patient_id: string
  first_touch: Record<string, string | null> | null
  profile: Profile
}
