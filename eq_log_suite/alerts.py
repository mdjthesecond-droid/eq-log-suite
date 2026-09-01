"""Alert/warning engine: evaluates every freshly-parsed event against the
user-editable `alert_rules` table and dispatches reactions. Rules are data,
not code -- add/edit/disable them from the web UI's /alerts page.
"""

import json
import re
import subprocess
import time

FIELDS = {"event_type", "source_name", "source_type", "target_name", "target_type", "verb", "amount", "outcome"}
# Value is a quoted string (may contain spaces, e.g. "Lucky Gambit") or a
# bare non-whitespace token -- quoted must come first so the alternation
# doesn't stop at the first space inside the quotes.
_COND_RE = re.compile(r"""(?P<field>\w+)\s*(?P<op>=|!=|>=|<=|>|<)\s*(?P<value>"[^"]*"|'[^']*'|[^\s]+)""")


def _coerce(field, value_str, actual):
    value_str = value_str.strip("'\"")
    if field == "amount":
        try:
            return float(value_str), (float(actual) if actual is not None else None)
        except ValueError:
            return None, None
    if isinstance(actual, str):
        actual = actual.lower()
    return value_str.lower(), actual


def _eval_single(field, op, value_str, event) -> bool:
    if field not in FIELDS:
        return False
    actual = getattr(event, field, None)
    if actual is None:
        return False
    value, actual = _coerce(field, value_str, actual)
    if value is None:
        return False
    return {
        "=": actual == value, "!=": actual != value,
        ">": actual > value, "<": actual < value,
        ">=": actual >= value, "<=": actual <= value,
    }.get(op, False)


def evaluate_condition(expr: str, event) -> bool:
    """Whitelisted mini-language, deliberately not `eval`/`exec` since rules
    come from a web form: `field OP value (AND field OP value)*`, e.g.
    "target_type=you AND amount>200" or "event_type=spell_cast AND source_type=npc".
    """
    for clause in re.split(r"\s+AND\s+", expr.strip(), flags=re.IGNORECASE):
        m = _COND_RE.match(clause.strip())
        if not m or not _eval_single(m.group("field"), m.group("op"), m.group("value"), event):
            return False
    return True


class _SafeDict(dict):
    """Leaves an unresolvable {placeholder} in overlay_text as literal text
    instead of raising -- a typo'd field name shouldn't break the whole
    reaction, just that one placeholder."""

    def __missing__(self, key):
        return "{" + key + "}"


class AlertEngine:
    def __init__(self, conn, game_id, broadcast=None, rule_refresh_seconds=15):
        self.conn = conn
        self.game_id = game_id
        self.broadcast = broadcast or (lambda obj: None)
        self.rule_refresh_seconds = rule_refresh_seconds
        self._rules = []
        self._last_refresh = 0.0
        self._last_fired: dict[int, float] = {}
        self._refresh_rules()

    def _refresh_rules(self):
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM alert_rules WHERE enabled=1 AND (game_id IS NULL OR game_id=%s)",
                (self.game_id,),
            )
            self._rules = cur.fetchall()
        self._last_refresh = time.monotonic()

    def evaluate(self, event):
        if time.monotonic() - self._last_refresh > self.rule_refresh_seconds:
            self._refresh_rules()

        for rule in self._rules:
            matched_text = self._match(rule, event)
            if matched_text is None:
                continue
            last = self._last_fired.get(rule["id"], 0.0)
            if time.monotonic() - last < (rule["cooldown_seconds"] or 0):
                continue
            self._last_fired[rule["id"]] = time.monotonic()
            self._fire(rule, event, matched_text)

    def _match(self, rule, event):
        if rule["match_type"] == "regex":
            m = re.search(rule["pattern"], event.raw_line)
            return m.group(0) if m else None
        if rule["match_type"] == "field_condition":
            return event.raw_line if evaluate_condition(rule["pattern"], event) else None
        return None

    def _fire(self, rule, event, matched_text):
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO alert_log (rule_id, event_id, ts, matched_text) VALUES (%s,%s,%s,%s)",
                (rule["id"], getattr(event, "db_id", None), event.ts, matched_text),
            )
        self.conn.commit()

        reactions = set((rule["reaction_types"] or "").split(","))
        config = json.loads(rule["reaction_config"]) if rule["reaction_config"] else {}

        if "notify" in reactions:
            subprocess.Popen(["notify-send", rule["name"], matched_text])
        if "sound" in reactions and config.get("sound_file"):
            subprocess.Popen(["paplay", config["sound_file"]])
        if "overlay" in reactions:
            text = config.get("overlay_text", matched_text)
            fields = _SafeDict(
                source_name=event.source_name, source_type=event.source_type,
                target_name=event.target_name, target_type=event.target_type,
                verb=event.verb, amount=event.amount, outcome=event.outcome,
                matched_text=matched_text,
            )
            self.broadcast({
                "kind": "alert",
                # Which on-screen box this renders into. Defaults to a box
                # implicitly owned by this rule alone (unchanged legacy
                # behavior) -- but if the user assigned this rule to a
                # fixed box via /alerts (reaction_config["box"], see
                # eq_log_suite/overlay/boxes.py), several rules can share
                # one box, replacing its content in place rather than each
                # rule getting its own.
                "key": config.get("box") or f"rule_{rule['id']}",
                "text": text.format_map(fields),
                "color": config.get("color", [0.9, 0.3, 0.2]),
                "duration": config.get("duration_seconds", 6),
                # When set, duration above doubles as a countdown length --
                # consumers render "{text} - Ns" ticking down to 0, rather
                # than showing static text for that long. The number comes
                # from the user's own knowledge of the ability (cast time,
                # discipline duration, etc.) -- not derivable from the log.
                "countdown": bool(config.get("countdown")),
            })
        # 'log' is implicit -- the alert_log row above always happens.
