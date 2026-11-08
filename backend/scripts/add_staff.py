"""Give a Supabase user a staff role at this clinic.

    .venv/bin/python scripts/add_staff.py nurse@clinic.example nurse "Nurse Tan"
    .venv/bin/python scripts/add_staff.py --list

The person must have signed in once at /staff (that creates their account).
Roles: staff (front desk: can acknowledge), nurse, clinician, admin (can review
and reply). Running it again changes the role. There's no UI for this on purpose:
granting access to patient data should be a deliberate admin action.
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv(Path(__file__).parent.parent / ".env")

from app.supabase_repo import connect  # noqa: E402

ROLES = ("staff", "nurse", "clinician", "admin")


def main():
    slug = os.environ.get("CLINIC_SLUG", "sunrise")
    with connect() as conn:
        if sys.argv[1:] == ["--list"]:
            rows = conn.execute(
                """select s.display_name, s.role, u.email from public.staff_member s
                   join auth.users u on u.id = s.auth_user_id
                   join public.clinic c on c.id = s.clinic_id where c.slug = %s order by s.created_at""",
                (slug,)).fetchall()
            for r in rows:
                print(f"{r['role']:10} {r['display_name']:24} {r['email']}")
            return
        if len(sys.argv) != 4 or sys.argv[2] not in ROLES:
            sys.exit(__doc__)
        email, role, name = sys.argv[1:]

        user = conn.execute(
            "select id from auth.users where lower(email) = lower(%s) and not coalesce(is_anonymous, false)",
            (email,)).fetchone()
        if user is None:
            sys.exit(f"No signed-in account for {email}. Ask them to sign in once at /staff first.")
        clinic = conn.execute("select id from public.clinic where slug = %s", (slug,)).fetchone()
        if clinic is None:
            sys.exit(f"Clinic {slug!r} not found")

        conn.execute(
            """insert into public.staff_member (auth_user_id, clinic_id, role, display_name)
               values (%s, %s, %s, %s)
               on conflict (auth_user_id) do update set role = excluded.role, display_name = excluded.display_name""",
            (user["id"], clinic["id"], role, name))
        conn.execute(
            "insert into public.audit_log (actor_type, action, target_table, metadata) values ('system', %s, 'staff_member', %s)",
            ("staff_role_granted", __import__("json").dumps({"role": role})))
        conn.commit()
        print(f"{email} is now {role} at {slug}.")


if __name__ == "__main__":
    main()
