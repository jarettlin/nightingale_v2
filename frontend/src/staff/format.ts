export const REASONS: Record<string, string> = {
  high_risk: 'Urgent: possible emergency',
  med_risk: 'Needs a nurse to assess',
  wants_diagnosis: 'Asked for a diagnosis',
  medication_change: 'Medication question',
  patient_request: 'Patient asked for the clinic',
}

export const CHANNELS: Record<string, string> = {
  instagram_ad_click: 'Instagram ad',
  google_ad_click: 'Google ad',
  instagram_comment: 'Instagram comment',
  tiktok_comment: 'TikTok comment',
  facebook_comment: 'Facebook comment',
  staff_referral: 'Staff referral',
  lead_form: 'Lead form',
  google_reviews: 'Google Reviews',
  website_widget: 'Website chat',
  direct: 'Direct link',
}

export const KINDS: Record<string, string> = {
  chief_complaint: 'Main concern', goal: 'Looking into', symptom: 'Symptom', medication: 'Medication',
  allergy: 'Allergy', condition: 'Condition', procedure: 'Procedure', other: 'Other',
}

export function ago(iso: string): string {
  const minutes = Math.round((Date.now() - new Date(iso).getTime()) / 60000)
  if (minutes < 1) return 'just now'
  if (minutes < 60) return `${minutes} min ago`
  const hours = Math.round(minutes / 60)
  return hours < 48 ? `${hours} h ago` : `${Math.round(hours / 24)} days ago`
}

export function due(iso: string): { text: string; overdue: boolean } {
  const minutes = Math.round((new Date(iso).getTime() - Date.now()) / 60000)
  const span = (m: number) => (Math.abs(m) < 60 ? `${Math.abs(m)} min` : `${Math.round(Math.abs(m) / 60)} h`)
  return minutes >= 0 ? { text: `reply due in ${span(minutes)}`, overdue: false }
    : { text: `overdue by ${span(minutes)}`, overdue: true }
}

export function time(iso: string): string {
  return new Date(iso).toLocaleString(undefined, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })
}
