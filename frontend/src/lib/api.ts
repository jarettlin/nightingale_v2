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
  const { data } = await supabase.auth.getSession()
  const headers: Record<string, string> = { 'content-type': 'application/json' }
  if (data.session) headers.authorization = `Bearer ${data.session.access_token}`
  const res = await fetch(`${BASE}${path}`, { method, headers, body: body ? JSON.stringify(body) : undefined })
  if (res.status === 204) return undefined as T
  const json = await res.json().catch(() => ({}))
  if (!res.ok) throw new ApiError(res.status, json.detail ?? json)
  return json as T
}

export const api = {
  clinic: () => request<ClinicInfo>('GET', '/clinic'),
  createSession: (params: { source?: string; campaign?: string; creative?: string; landing_page?: string }) =>
    request<{ session: { id: string; source_channel: string }; greeting: string; emergency_notice: string }>(
      'POST', '/sessions', params),
  messages: (sid: string) => request<{ messages: ChatMessage[] }>('GET', `/sessions/${sid}/messages`),
  profile: (sid: string) => request<{ profile: Profile }>('GET', `/sessions/${sid}/profile`),
  send: (sid: string, text: string) => request<MessageResponse>('POST', `/sessions/${sid}/messages`, { text }),
  authStarted: (sid: string) => request<void>('POST', `/sessions/${sid}/auth-started`),
  convert: (sid: string, body: { phone: string; data_sharing: boolean; marketing: boolean; exclude_memory_ids: string[] }) =>
    request<{ patient_id: string; opening_message: string; profile: Profile }>('POST', `/sessions/${sid}/convert`, body),
  escalate: (sid: string) =>
    request<{ escalation_id: string; confirmation: string; already_open: boolean; response_due_at: string }>(
      'POST', `/sessions/${sid}/escalations`, {}),
}
