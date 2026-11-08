"""Channel rules: how a conversation opens, from channel_rules.yaml.

All channel behaviour lives in that one file. This module only evaluates it:
which rule matches, whether the clinic is open, and what not to ask again.
"""

import re
import string
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from app.config import ClinicConfig

RULES_FILE = Path(__file__).parent.parent / "channel_rules.yaml"
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


@dataclass
class Opening:
    greeting: str
    rule_id: str
    time_of_day: str                       # in_hours | after_hours
    skip: list[str] = field(default_factory=list)
    private_reply: str | None = None       # for comment channels: the DM template, {link} still unfilled


@lru_cache(maxsize=4)
def load_rules(path: Path = RULES_FILE) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# Staff naturally write "asked about egg freezing…"; the greeting already says "you asked about"
NOTE_LEAD_IN = re.compile(r"^(?:(?:the )?(?:patient|she|he|they) )?(?:asked|was asking|enquired|inquired|"
                          r"wanted to know|is interested|was interested) (?:about|in)\s+", re.I)


def topic_for(session, rules: dict | None = None) -> str | None:
    """What this person arrived asking about: a staff note, a comment topic, or the ad they clicked."""
    rules = rules or load_rules()
    if session.referral_topic:
        return NOTE_LEAD_IN.sub("", session.referral_topic.strip()) or session.referral_topic
    if session.campaign_id:
        return rules.get("campaign_topics", {}).get(session.campaign_id)
    return None


def _matches(when: dict, facts: dict) -> bool:
    for key, expected in when.items():
        allowed = expected if isinstance(expected, list) else [expected]
        if facts.get(key) not in allowed:
            return False
    return True


def _fillable(template: str, values: dict) -> bool:
    names = {name for _, name, _, _ in string.Formatter().parse(template) if name}
    return all(values.get(n) for n in names - {"link"})


def clinic_status(now: datetime, rules: dict | None = None) -> tuple[str, str]:
    """('in_hours' | 'after_hours', when the nurses are next in, e.g. 'tomorrow at 08:30')."""
    rules = rules or load_rules()
    hours = rules["clinic_hours"]
    local = now.astimezone(ZoneInfo(hours["timezone"]))
    today = hours["open"].get(local.weekday())
    if today and today[0] <= local.strftime("%H:%M") < today[1]:
        return "in_hours", "now"
    for ahead in range(0, 8):
        day = local + timedelta(days=ahead)
        slot = hours["open"].get(day.weekday())
        if not slot or (ahead == 0 and local.strftime("%H:%M") >= slot[0]):
            continue
        when = "today" if ahead == 0 else "tomorrow" if ahead == 1 else DAYS[day.weekday()]
        return "after_hours", f"{when} at {slot[0]}"
    return "after_hours", "soon"


def opening(session, clinic: ClinicConfig, now: datetime, rules: dict | None = None) -> Opening:
    rules = rules or load_rules()
    time_of_day, next_open = clinic_status(now, rules)
    facts = {"channel": session.source_channel, "identity_level": session.identity_level, "time_of_day": time_of_day}
    values = {"clinic": clinic.name, "topic": topic_for(session, rules), "next_open": next_open}

    for rule in rules["rules"]:
        if _matches(rule.get("when", {}), facts) and _fillable(rule["greeting"], values):
            greeting = rule["greeting"].format(**{**values, "link": ""})
            note = rules.get("time_of_day", {}).get(time_of_day)
            if note:
                greeting = f"{greeting} {note.format(**values)}"
            reply = rule.get("private_reply")
            return Opening(greeting=greeting, rule_id=rule["id"], time_of_day=time_of_day,
                           skip=list(rule.get("skip", [])),
                           private_reply=reply.replace("{topic}", values["topic"] or "") if reply else None)
    raise ValueError("channel_rules.yaml needs a catch-all rule with `when: {}`")
