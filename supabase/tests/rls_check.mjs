// Applies the migrations to an in-memory Postgres (PGlite) and checks who can
// see what. Run: npm install && npm run check   (from supabase/tests)
import { PGlite } from "@electric-sql/pglite";
import { readFileSync, readdirSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const migrations = join(here, "..", "migrations");
const db = new PGlite();

await db.exec(readFileSync(join(here, "supabase_stubs.sql"), "utf8"));
for (const file of readdirSync(migrations).filter((f) => f.endsWith(".sql")).sort()) {
  await db.exec(readFileSync(join(migrations, file), "utf8"));
}
await db.exec(readFileSync(join(here, "..", "seed.sql"), "utf8"));

// ---------------------------------------------------------------- fixtures
const id = {};
const uuid = async (name) => (id[name] = (await db.query("select gen_random_uuid() as u")).rows[0].u);
for (const n of ["guestG", "patientA", "patientB", "nurse", "otherStaff"]) await uuid(n);

const one = async (sql, params = []) => (await db.query(sql, params)).rows[0];
const clinic = (await one("select id from clinic where slug = 'sunrise'")).id;
const otherClinic = (await one(
  "insert into clinic (slug, name, emergency_number) values ('other', 'Other Clinic', '995') returning id")).id;

await db.query("insert into auth.users (id, is_anonymous) values ($1, true), ($2, false), ($3, false), ($4, false), ($5, false)",
  [id.guestG, id.patientA, id.patientB, id.nurse, id.otherStaff]);
await db.query("insert into staff_member (auth_user_id, clinic_id, role, display_name) values ($1, $2, 'nurse', 'Nurse N'), ($3, $4, 'staff', 'Other S')",
  [id.nurse, clinic, id.otherStaff, otherClinic]);

const pA = (await one("insert into patient (auth_user_id, clinic_id) values ($1, $2) returning id", [id.patientA, clinic])).id;
const pB = (await one("insert into patient (auth_user_id, clinic_id) values ($1, $2) returning id", [id.patientB, clinic])).id;

// A was a guest first (same auth uid after converting), B arrived via staff referral, G is still a guest
const lead = async (guestUid, patient, channel, topic = null) => (await one(
  `insert into lead_session (clinic_id, guest_auth_user_id, patient_id, converted_at, source_channel,
                             campaign_id, identity_level, referral_topic)
   values ($1, $2, $3, case when $3::uuid is null then null else now() end, $4, 'ivf_over40',
           case when $4 = 'staff_referral' then 'staff_referred' else 'anonymous' end, $5)
   returning id`, [clinic, guestUid, patient, channel, topic])).id;
const LA = await lead(id.patientA, pA, "instagram_ad_click");
const LB = await lead(null, pB, "staff_referral", "asked about egg freezing at today's visit");
const LG = await lead(id.guestG, null, "website_widget");

const msg = async (session, sentAs, text) => (await one(
  `insert into message (lead_session_id, sender, sent_as, content_encrypted, content_redacted, risk_level, risk_reason, risk_confidence, risk_assessed_at)
   values ($1, 'patient', $2, 'ciphertext', $3, 'low', 'none', 'med', now()) returning id`, [session, sentAs, text])).id;
const mA1 = await msg(LA, "guest", "I take Advil");
const mA2 = await msg(LA, "patient", "Actually I stopped last week");
await msg(LB, "patient", "B's private message");
await msg(LG, "guest", "G's guest message");
await db.query("insert into lead_contact (lead_session_id, social_platform, social_handle) values ($1, 'instagram', '@guest_g')", [LG]);

const advil = (await one(
  "insert into memory_item (lead_session_id, kind, value, status, provenance_message_id) values ($1, 'medication', 'Advil', 'active', $2) returning id",
  [LA, mA1])).id;
await db.query(
  "insert into memory_item (lead_session_id, kind, value, status, provenance_message_id, supersedes_id) values ($1, 'medication', 'Advil', 'stopped', $2, $3)",
  [LA, mA2, advil]);

await db.query("insert into consent (clinic_id, lead_session_id, patient_id, kind, granted, policy_version) values ($1, $2, $3, 'data_sharing', true, 'v1')",
  [clinic, LA, pA]);
await db.query(
  `insert into escalation (clinic_id, lead_session_id, patient_id, trigger_message_id, reason, risk_level,
                           triage_summary, profile_snapshot, attribution, response_due_at)
   values ($1, $2, $3, $4, 'med_risk', 'med', '[{"text": "x", "provenance_message_ids": []}]', '[]', '{}', now() + interval '18 hours')`,
  [clinic, LA, pA, mA1]);

// ---------------------------------------------------------------- helpers
async function as(role, uid, fn) {
  await db.exec(`set role ${role}`);
  await db.query("select set_config('request.jwt.claims', $1, false)", [JSON.stringify(uid ? { sub: uid, role } : { role })]);
  try {
    return await fn();
  } finally {
    await db.exec("reset role");
    await db.query("select set_config('request.jwt.claims', '', false)");
  }
}
const count = async (sql, params = []) => Number((await one(`select count(*) as n from (${sql}) q`, params)).n);
const fails = async (fn) => { try { await fn(); return false; } catch { return true; } };

let failed = 0;
function check(name, ok) {
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}`);
  if (!ok) failed++;
}

// ---------------------------------------------------------------- checks
await as("authenticated", id.patientA, async () => {
  check("patient A sees only their own 2 messages", await count("select * from message") === 2);
  check("patient A cannot fetch patient B's messages", await count("select * from message where lead_session_id = $1", [LB]) === 0);
  check("patient A sees only their own patient row", await count("select * from patient") === 1);
  check("patient A cannot see the funnel or other leads", await count("select * from funnel_event") === 0
        && await count("select * from lead_session") === 1);
  check("patient A sees their escalation, not a queue", await count("select * from escalation") === 1);
  check("patient A cannot insert messages (writes are backend-only)", await fails(() =>
    db.query("insert into message (lead_session_id, sender, sent_as, content_redacted) values ($1, 'assistant', 'patient', 'forged')", [LA])));
  check("patient A cannot change their risk level", await fails(() => db.query("update message set risk_level = 'low'")));
  check("patient A cannot read the audit log", await fails(() => db.query("select * from audit_log")));
  check("current_memory shows Advil as stopped only", JSON.stringify((await db.query(
    "select value, status from current_memory")).rows) === JSON.stringify([{ value: "Advil", status: "stopped" }]));
  check("both memory states keep provenance links", await count(
    "select * from memory_item m join message g on g.id = m.provenance_message_id") === 2);
});

await as("authenticated", id.patientB, async () => {
  check("patient B cannot see patient A's messages or memory", await count("select * from message where lead_session_id = $1", [LA]) === 0
        && await count("select * from memory_item") === 0);
});

await as("authenticated", id.guestG, async () => {
  check("guest sees only their own session's messages", await count("select * from message") === 1);
  check("guest sees no patients", await count("select * from patient") === 0);
});

await as("anon", null, async () => {
  check("signed-out visitor can read clinic info", await count("select * from clinic") === 2);
  check("signed-out visitor is rejected from messages", await fails(() => db.query("select * from message")));
});

await as("authenticated", id.nurse, async () => {
  check("nurse sees consented patient A's messages", await count("select * from message where lead_session_id = $1", [LA]) === 2);
  check("nurse cannot see unconsented patient B's messages", await count("select * from message where lead_session_id = $1", [LB]) === 0);
  check("nurse cannot see guest messages", await count("select * from message where lead_session_id = $1", [LG]) === 0);
  check("nurse sees all 3 leads' metadata for the warm-lead view", await count("select * from lead_session") === 3);
  check("nurse cannot see who the guest is without consent", await count("select * from lead_contact") === 0);
  check("nurse sees the escalation queue", await count("select * from escalation") === 1);
});

await as("authenticated", id.otherStaff, async () => {
  check("staff at another clinic see nothing at this clinic", await count("select * from lead_session") === 0
        && await count("select * from message") === 0);
});

check("memory items cannot be edited in place", await fails(() => db.query("update memory_item set status = 'active'")));
await db.query("insert into audit_log (actor_type, action) values ('system', 'check')");
check("audit log cannot be deleted", await fails(() => db.query("delete from audit_log")));
check("audit log cannot be edited", await fails(() => db.query("update audit_log set action = 'x'")));

// Withdrawal: a newer granted=false row wins
await db.query("insert into consent (clinic_id, patient_id, kind, granted, policy_version, recorded_at) values ($1, $2, 'data_sharing', false, 'v1', now() + interval '1 second')",
  [clinic, pA]);
await as("authenticated", id.nurse, async () => {
  check("after consent withdrawal the nurse loses access", await count("select * from message") === 0);
});

// Purge: expired guest content goes, metadata stays, converted patients untouched
await db.query("update lead_session set expires_at = now() - interval '1 day' where id in ($1, $2)", [LG, LA]);
const purged = (await one("select app.purge_expired_guest_data() as n")).n;
check("purge removes only the unconverted guest session", purged === 1);
check("guest messages are gone, lead metadata kept", await count("select * from message where lead_session_id = $1", [LG]) === 0
      && await count("select * from lead_session where id = $1 and purged_at is not null", [LG]) === 1);
check("converted patient's messages survive the purge", await count("select * from message where lead_session_id = $1", [LA]) === 2);
check("authenticated users cannot run the purge", await as("authenticated", id.patientA, () =>
  fails(() => db.query("select app.purge_expired_guest_data()"))));

console.log(failed ? `\n${failed} check(s) failed` : "\nall checks passed");
process.exit(failed ? 1 : 0);
