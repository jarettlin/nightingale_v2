"""Manage clinic staff. Staff accounts are invite-only: this is the only way in.

    .venv/bin/python scripts/add_staff.py add nurse@clinic.example nurse "Nurse Tan"
    .venv/bin/python scripts/add_staff.py remove nurse@clinic.example
    .venv/bin/python scripts/add_staff.py list

add:    invites the person if they have no account yet (they get an email whose link
        signs them in at /staff), then gives them the role. Running it again changes
        the role. Roles: staff (front desk: acknowledges), nurse, clinician, admin
        (review and reply).
remove: takes the role away. Access ends on their next request; their account stays.

Inviting needs SUPABASE_SECRET_KEY in backend/.env (Supabase → Project Settings →
API Keys → secret key). It bypasses all security rules: keep it out of git and
out of the frontend.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
load_dotenv(Path(__file__).parent.parent / ".env")

from app.staff_admin import InviteError, invite_user  # noqa: E402
from app.supabase_repo import connect  # noqa: E402

ROLES = ("staff", "nurse", "clinician", "admin")
FRONTEND = os.environ.get("FRONTEND_URL", "http://localhost:5173")


def audit(conn, action: str, **metadata):
    conn.execute("insert into public.audit_log (actor_type, action, target_table, metadata) "
                 "values ('system', %s, 'staff_member', %s)", (action, json.dumps(metadata)))


def find_user(conn, email: str):
    return conn.execute("select id from auth.users where lower(email) = lower(%s) "
                        "and not coalesce(is_anonymous, false)", (email,)).fetchone()


def add(conn, clinic_id, email: str, role: str, name: str):
    user = find_user(conn, email)
    if user:
        user_id = user["id"]
        print(f"{email} already has an account.")
    else:
        key = os.environ.get("SUPABASE_SECRET_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        if not key:
            sys.exit("To invite someone new, add SUPABASE_SECRET_KEY to backend/.env "
                     "(Supabase → Project Settings → API Keys → secret key).")
        try:
            user_id = invite_user(email, os.environ["SUPABASE_URL"], key, redirect_to=f"{FRONTEND}/staff")
        except InviteError as exc:
            sys.exit(str(exc))
        print(f"Invited {email}: they'll get an email with a sign-in link.")

    conn.execute(
        """insert into public.staff_member (auth_user_id, clinic_id, role, display_name) values (%s, %s, %s, %s)
           on conflict (auth_user_id) do update set role = excluded.role, display_name = excluded.display_name""",
        (user_id, clinic_id, role, name))
    audit(conn, "staff_role_granted", role=role)
    print(f"{email} is now {role}.")


def remove(conn, email: str):
    user = find_user(conn, email)
    deleted = conn.execute("delete from public.staff_member where auth_user_id = %s returning id",
                           (user["id"],)).fetchone() if user else None
    if not deleted:
        sys.exit(f"{email} has no staff access.")
    audit(conn, "staff_role_revoked")
    print(f"{email} no longer has staff access.")


def list_staff(conn, clinic_id):
    rows = conn.execute(
        """select s.role, s.display_name, u.email, u.last_sign_in_at is not null as signed_in
           from public.staff_member s join auth.users u on u.id = s.auth_user_id
           where s.clinic_id = %s order by s.created_at""", (clinic_id,)).fetchall()
    for r in rows:
        print(f"{r['role']:10} {r['display_name']:24} {r['email']}{'' if r['signed_in'] else '  (invite not used yet)'}")
    if not rows:
        print("No staff yet.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    a = sub.add_parser("add")
    a.add_argument("email")
    a.add_argument("role", choices=ROLES)
    a.add_argument("name")
    r = sub.add_parser("remove")
    r.add_argument("email")
    sub.add_parser("list")
    args = parser.parse_args()

    slug = os.environ.get("CLINIC_SLUG", "sunrise")
    with connect() as conn:
        clinic = conn.execute("select id from public.clinic where slug = %s", (slug,)).fetchone()
        if clinic is None:
            sys.exit(f"Clinic {slug!r} not found")
        if args.command == "add":
            add(conn, clinic["id"], args.email, args.role, args.name)
        elif args.command == "remove":
            remove(conn, args.email)
        else:
            list_staff(conn, clinic["id"])
        conn.commit()


if __name__ == "__main__":
    main()
