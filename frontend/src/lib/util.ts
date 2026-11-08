/** localStorage for per-device conveniences only (which conversation to resume).
 *  It can be unavailable (private mode), so every access is guarded. */
const PREFIX = 'nightingale.'

export const store = {
  get(key: string): string | null {
    try { return window.localStorage.getItem(PREFIX + key) } catch { return null }
  },
  set(key: string, value: string) {
    try { window.localStorage.setItem(PREFIX + key, value) } catch { /* not available */ }
  },
  remove(key: string) {
    try { window.localStorage.removeItem(PREFIX + key) } catch { /* not available */ }
  },
}

export const LABELS_SHORT: Record<string, string> = {
  chief_complaint: 'Main concern',
  goal: 'Looking into',
  symptom: 'Symptom',
  medication: 'Medication',
  allergy: 'Allergy',
  condition: 'Condition',
  procedure: 'Procedure',
  other: 'Other',
}
