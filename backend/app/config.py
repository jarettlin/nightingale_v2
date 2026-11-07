"""Per-clinic settings. Later this moves to a `clinic` table in Supabase."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ClinicConfig:
    clinic_id: str
    name: str
    emergency_number: str  # 999 in Malaysia, 995 in Singapore: never hardcode it
    response_window: str


DEFAULT_CLINIC = ClinicConfig(
    clinic_id="clinic_demo",
    name="Sunrise Fertility Clinic",
    emergency_number="999",
    response_window="12 to 18 hours",
)
