"""Parser for classic-format EverQuest logs (EverQuest Legends and any other
EQ-client-derived game using the standard `[Day Mon DD HH:MM:SS YYYY] text`
log format). Patterns were derived from a full scan of a real 34MB log
(eqlog_Cheerful_rivervale.txt) rather than guessed, but coverage is not
exhaustive -- unmatched lines simply aren't decomposed into `events` and
still land in `raw_lines`, so nothing is lost and coverage can grow later.
"""

import re

from eq_log_suite.models import ParsedEvent
from eq_log_suite.parsers.base import GameParser

# Third-person conjugated melee verbs seen (or expected) in combat lines,
# e.g. "Gonekab crushes Baron Telyx V`Zher for 13 points of damage."
# Extend this list as new verbs show up in unmatched raw_lines.
MELEE_VERBS_3P = (
    "hits", "crushes", "slashes", "pierces", "cleaves", "bashes", "kicks",
    "punches", "bites", "backstabs", "claws", "stings", "gores", "mauls",
    "rends", "gouges", "smashes", "strikes",
)
MELEE_VERB_ALT = "|".join(MELEE_VERBS_3P)

# Damage-dealing lines report the attacker's verb conjugated 3rd-person
# ("crushes"); miss/evade lines report it as the bare/2nd-person form
# ("crush", as in "tries to crush YOU"). Normalized to the bare form so a
# single verb (e.g. "crush") aggregates hit+miss+evade for the same attack
# type into one row instead of splitting into "crushes" vs "crush".
MELEE_VERB_3P_TO_BASE = dict(zip(MELEE_VERBS_3P, (
    "hit", "crush", "slash", "pierce", "cleave", "bash", "kick", "punch",
    "bite", "backstab", "claw", "sting", "gore", "maul", "rend", "gouge",
    "smash", "strike",
)))

DODGE_PARRY_ALT = "dodges|parries|blocks|ripostes"

# Second-person verbs seen in "X tries to VERB YOU, ..." lines (an incoming
# attack on you, whether it lands, misses, or you evade it). Listed longest
# first so a compound verb like "frenzy on" is tried before any shorter verb
# that happens to be a prefix of it.
INCOMING_ATTACK_VERBS = (
    "backstab", "bash", "bite", "cleave", "crush", "frenzy on", "hit",
    "kick", "maul", "pierce", "punch", "slash", "slice", "smash", "strike",
    "shoot",
)
INCOMING_ATTACK_VERB_ALT = "|".join(sorted(INCOMING_ATTACK_VERBS, key=len, reverse=True))


def _amount(m, name="amount"):
    return int(m.group(name))


def h_melee_self(m):
    tag = m.group("tag")
    outcome = "crit" if tag == "Critical" else ("riposte" if tag == "Riposte" else "hit")
    return ParsedEvent(
        ts=None, raw_line=None, event_type="melee",
        source_name="You", source_type="you",
        target_name=m.group("target"), target_type="npc",
        verb=m.group("verb"), amount=_amount(m), outcome=outcome,
    )


def h_melee_self_typed(m):
    return ParsedEvent(
        ts=None, raw_line=None, event_type="melee",
        source_name="You", source_type="you",
        target_name=m.group("target"), target_type="npc",
        verb=m.group("spell"), amount=_amount(m), outcome="hit",
        extra={"damage_type": m.group("dmgtype"), "proc": True},
    )


def h_melee_other_typed(m):
    source = m.group("source")
    target = m.group("target")
    return ParsedEvent(
        ts=None, raw_line=None, event_type="melee",
        source_name=source, source_type=GameParser.classify_actor(source),
        target_name=target, target_type=GameParser.classify_actor(target),
        verb=m.group("spell"), amount=_amount(m), outcome="hit",
        extra={"damage_type": m.group("dmgtype"), "proc": True},
    )


def h_melee_other(m):
    tag = m.group("tag")
    outcome = "crit" if tag == "Critical" else ("riposte" if tag == "Riposte" else "hit")
    source = m.group("source")
    target = m.group("target")
    verb = m.group("verb")
    return ParsedEvent(
        ts=None, raw_line=None, event_type="melee",
        source_name=source, source_type=GameParser.classify_actor(source),
        target_name=target, target_type=GameParser.classify_actor(target),
        verb=MELEE_VERB_3P_TO_BASE.get(verb, verb), amount=_amount(m), outcome=outcome,
    )


def h_miss_self(m):
    return ParsedEvent(
        ts=None, raw_line=None, event_type="melee",
        source_name="You", source_type="you",
        target_name=m.group("target"), target_type="npc",
        verb=m.group("verb"), amount=None, outcome="miss",
    )


_AVOID_OUTCOME = {"dodges": "dodge", "parries": "parry", "blocks": "block", "ripostes": "riposte"}


def h_miss_self_avoided(m):
    return ParsedEvent(
        ts=None, raw_line=None, event_type="melee",
        source_name="You", source_type="you",
        target_name=m.group("target"), target_type="npc",
        verb=m.group("verb"), amount=None,
        outcome=_AVOID_OUTCOME.get(m.group("outcome"), m.group("outcome")),
    )


def h_miss_other(m):
    source = m.group("source")
    target = m.group("target")
    return ParsedEvent(
        ts=None, raw_line=None, event_type="melee",
        source_name=source, source_type=GameParser.classify_actor(source),
        target_name=target, target_type=GameParser.classify_actor(target),
        verb=m.group("verb"), amount=None, outcome="miss",
    )


_INCOMING_EVADE_OUTCOME = {"dodge": "dodge", "parry": "parry", "riposte": "riposte", "block": "block"}


def h_evade_incoming(m):
    # "A grikbar kobold tries to hit YOU, but YOU dodge!" -- your own
    # evasion against an incoming attack. Only reported for you (the log
    # owner), never for other players, so target is always literally YOU.
    source = m.group("source")
    return ParsedEvent(
        ts=None, raw_line=None, event_type="melee",
        source_name=source, source_type=GameParser.classify_actor(source),
        target_name="You", target_type="you",
        verb=m.group("verb"), amount=None,
        outcome=_INCOMING_EVADE_OUTCOME.get(m.group("outcome"), m.group("outcome")),
    )


def _heal_extra(m):
    # "You healed X for 5 (10) hit points by Spell." -- 5 is what the target
    # actually gained (capped by their missing HP), 10 is what the spell
    # would have healed for with a large enough deficit. No parenthetical at
    # all means no cap was hit -- actual and potential are the same.
    potential = int(m.group("potential")) if m.group("potential") else _amount(m)
    return {"potential": potential, "overheal": potential - _amount(m)}


def h_heal(m):
    return ParsedEvent(
        ts=None, raw_line=None, event_type="spell_heal",
        source_name="You", source_type="you",
        target_name=m.group("target"), target_type="unknown",
        verb=m.group("spell"), amount=_amount(m), outcome="hit",
        extra=_heal_extra(m),
    )


def h_heal_other(m):
    source = m.group("source")
    target = m.group("target")
    return ParsedEvent(
        ts=None, raw_line=None, event_type="spell_heal",
        source_name=source, source_type=GameParser.classify_actor(source),
        target_name=target, target_type=GameParser.classify_actor(target),
        verb=m.group("spell"), amount=_amount(m), outcome="hit",
        extra=_heal_extra(m),
    )


def h_taken_from_your(m):
    target = m.group("target")
    return ParsedEvent(
        ts=None, raw_line=None, event_type="spell_damage",
        source_name="You", source_type="you",
        target_name=target, target_type=GameParser.classify_actor(target),
        verb=m.group("spell"), amount=_amount(m), outcome="hit",
    )


def h_taken_from_others(m):
    target = m.group("target")
    source = m.group("source")
    return ParsedEvent(
        ts=None, raw_line=None, event_type="spell_damage",
        source_name=source, source_type=GameParser.classify_actor(source),
        target_name=target, target_type=GameParser.classify_actor(target),
        verb=m.group("spell"), amount=_amount(m), outcome="hit",
    )


def h_taken_from_by(m):
    # "You have taken 6 damage from Flame Lick by orc taskmaster." -- an
    # alternate word order from h_taken_from_your/h_taken_from_others
    # ("X has taken N damage from your/SOURCE'S spell"): here the spell
    # comes first and the source is introduced with "by" instead of a
    # possessive, but it's the same underlying event (incoming spell/proc
    # damage), just always targeting you specifically.
    source = m.group("source")
    return ParsedEvent(
        ts=None, raw_line=None, event_type="spell_damage",
        source_name=source, source_type=GameParser.classify_actor(source),
        target_name="You", target_type="you",
        verb=m.group("spell"), amount=_amount(m), outcome="hit",
    )


# "Non-melee damage" here is always a damage shield -- the reflect-style
# buff that hurts whoever melees its owner. The source text names the DS's
# owner, not an independent actor: "a gloomwater mermaid's thorns" is that
# mermaid's damage shield, "YOUR flames" is your own. Resolving to the real
# owner name (instead of leaving "X's thorns" as a standalone pseudo-actor)
# lets this damage merge into that owner's own row everywhere else.
_DS_YOUR_RE = re.compile(r"^YOUR (?:thorns|flames)$")
_DS_OWNER_RE = re.compile(r"^(?P<owner>.+?)'s (?:thorns|flames)$")


def _damage_shield_owner(source_text):
    if _DS_YOUR_RE.match(source_text):
        return "You", "you", True
    m = _DS_OWNER_RE.match(source_text)
    if m:
        owner = m.group("owner")
        return owner, GameParser.classify_actor(owner), True
    return source_text, GameParser.classify_actor(source_text), False


def h_nonmelee_you(m):
    owner, owner_type, is_ds = _damage_shield_owner(m.group("source"))
    extra = {"non_melee": True}
    if is_ds:
        extra["damage_shield"] = True
    return ParsedEvent(
        ts=None, raw_line=None, event_type="spell_damage",
        source_name=owner, source_type=owner_type,
        target_name="You", target_type="you",
        verb=m.group("verb"), amount=_amount(m), outcome="hit",
        extra=extra,
    )


def h_nonmelee_other(m):
    target = m.group("target")
    owner, owner_type, is_ds = _damage_shield_owner(m.group("source"))
    extra = {"non_melee": True}
    if is_ds:
        extra["damage_shield"] = True
    return ParsedEvent(
        ts=None, raw_line=None, event_type="spell_damage",
        source_name=owner, source_type=owner_type,
        target_name=target, target_type=GameParser.classify_actor(target),
        verb=m.group("verb"), amount=_amount(m), outcome="hit",
        extra=extra,
    )


def h_cast_begin(m):
    source = m.group("source")
    return ParsedEvent(
        ts=None, raw_line=None, event_type="spell_cast",
        source_name=source, source_type=GameParser.classify_actor(source),
        target_name=None, target_type=None,
        verb=m.group("spell"), amount=None, outcome=None,
    )


def h_slain_self(m):
    return ParsedEvent(
        ts=None, raw_line=None, event_type="death",
        source_name="You", source_type="you",
        target_name=m.group("target"), target_type="npc",
        verb=None, amount=None, outcome=None,
    )


def h_slain_other(m):
    source = m.group("source")
    return ParsedEvent(
        ts=None, raw_line=None, event_type="death",
        source_name=source, source_type=GameParser.classify_actor(source),
        target_name=m.group("target"), target_type="npc",
        verb=None, amount=None, outcome=None,
    )


def h_slain_by(m):
    source = m.group("source")
    return ParsedEvent(
        ts=None, raw_line=None, event_type="death",
        source_name=source, source_type=GameParser.classify_actor(source),
        target_name=m.group("target"), target_type="unknown",
        verb=None, amount=None, outcome=None,
    )


def h_you_died(m):
    # "You died." doesn't say what killed you -- source is left unknown.
    return ParsedEvent(
        ts=None, raw_line=None, event_type="death",
        source_name=None, source_type=None,
        target_name="You", target_type="you",
        verb=None, amount=None, outcome=None,
    )


def h_escape(m):
    return ParsedEvent(
        ts=None, raw_line=None, event_type="escape",
        source_name="You", source_type="you",
        target_name=None, target_type=None,
        verb=None, amount=None, outcome=None,
    )


def h_resisted(m):
    target = m.group("target")
    return ParsedEvent(
        ts=None, raw_line=None, event_type="spell_resist",
        source_name="You", source_type="you",
        target_name=target, target_type=GameParser.classify_actor(target),
        verb=m.group("spell"), amount=None, outcome="resist",
    )


def h_struck_by_generic(m):
    target = m.group("target")
    return ParsedEvent(
        ts=None, raw_line=None, event_type="spell_effect",
        source_name=None, source_type=None,
        target_name=target, target_type=GameParser.classify_actor(target),
        verb=m.group("desc"), amount=None, outcome=None,
    )


# EQL instanced zones append a difficulty tier to the zone name, e.g.
# "Permafrost Keep 4 (Refined)" (base zone "Permafrost Keep", tier 4, label
# "Refined") -- confirmed against real zone names: 1=Awakened, 2=Adaptive,
# 4=Refined (3 not seen yet, presumably "Restored" or similar). No trailing
# number means the base/open-world difficulty, tier 0. A "- Solo" marker
# means "solo raid" (a raid instance sized for one player), not "you were
# alone" -- it can appear with or without a tier suffix ("Kedge Keep - Solo"
# vs "Nagafen's Lair - Solo 1 (Awakened)") and is tracked separately from
# the tier number.
_ZONE_TIER_RE = re.compile(r"^(?P<base>.+?) (?P<tier>\d+) \((?P<tier_label>[^)]+)\)$")
_ZONE_SOLO_SUFFIX_RE = re.compile(r"^(?P<base>.+?) - Solo$")


def parse_zone_tier(zone_name):
    """-> (base_zone, tier: int, solo: bool, tier_label: str|None)"""
    name = zone_name
    tier, tier_label = 0, None
    m = _ZONE_TIER_RE.match(name)
    if m:
        name = m.group("base")
        tier = int(m.group("tier"))
        tier_label = m.group("tier_label")
    solo = False
    m = _ZONE_SOLO_SUFFIX_RE.match(name)
    if m:
        name = m.group("base")
        solo = True
    return name, tier, solo, tier_label


# "You have entered <zone>." also fires for a handful of area-flag notices
# that share the exact same sentence shape but aren't a real zone
# transition -- e.g. "...an area where levitation effects do not function."
# on crossing into a no-fly section of whatever zone you're already in.
# Left unmatched (falls through to raw_lines) rather than overwriting the
# real current zone for anything correlated against zone_change afterward.
_ZONE_CHANGE_FALSE_POSITIVES = {
    "an area where levitation effects do not function",
}


def h_zone_change(m):
    zone = m.group("zone")
    if zone in _ZONE_CHANGE_FALSE_POSITIVES:
        return None
    base_zone, tier, solo, tier_label = parse_zone_tier(zone)
    extra = {"base_zone": base_zone, "tier": tier, "solo": solo}
    if tier_label:
        extra["tier_label"] = tier_label
    return ParsedEvent(
        ts=None, raw_line=None, event_type="zone_change",
        source_name="You", source_type="you",
        target_name=zone, target_type="zone",
        verb=None, amount=None, outcome=None,
        extra=extra,
    )


def h_exp(m):
    return ParsedEvent(
        ts=None, raw_line=None, event_type="exp",
        source_name="You", source_type="you",
        target_name=None, target_type=None,
        verb=None, amount=None, outcome=None,
        extra={"percent": float(m.group("pct"))},
    )


def h_currency(m):
    return ParsedEvent(
        ts=None, raw_line=None, event_type="currency",
        source_name=None, source_type="npc",
        target_name="You", target_type="you",
        verb=None, amount=None, outcome=None,
        extra={"text": m.group("text")},
    )


def h_loot(m):
    qty = int(m.group("qty")) if m.group("qty") else 1
    return ParsedEvent(
        ts=None, raw_line=None, event_type="loot",
        source_name=m.group("source"), source_type="npc",
        target_name=m.group("item"), target_type="item",
        verb=None, amount=qty, outcome=None,
    )


def h_faction(m):
    return ParsedEvent(
        ts=None, raw_line=None, event_type="faction",
        source_name="You", source_type="you",
        target_name=m.group("faction"), target_type="faction",
        verb=None, amount=int(m.group("delta")), outcome=None,
    )


class EQLegendsParser(GameParser):
    game_code = "eql"

    PATTERNS = [
        # "You pierce a grikbar kobold for 6 points of damage."
        # "You pierce a greater kobold for 112 points of damage. (Critical)"
        (re.compile(
            r"^You (?P<verb>\w+) (?P<target>.+?) for (?P<amount>\d+) points? of damage\."
            r"(?: \((?P<tag>Critical|Riposte|Flurry|Strikethrough)\))?$"
        ), h_melee_self),

        # "You hit a grikbar kobold for 3 points of poison damage by Asp Venom Strike."
        (re.compile(
            r"^You hit (?P<target>.+?) for (?P<amount>\d+) points? of (?P<dmgtype>\w+) damage "
            r"by (?P<spell>.+?)\.$"
        ), h_melee_self_typed),

        # "Libarn hit a grikbar kobold for 8 points of magic damage by Strike."
        # "a grikbar shaman hit you for 4 points of fire damage by Burst of Flame."
        (re.compile(
            r"^(?P<source>.+?) hit (?P<target>.+?) for (?P<amount>\d+) points? of (?P<dmgtype>\w+) "
            r"damage by (?P<spell>.+?)\.$"
        ), h_melee_other_typed),

        # "Gonekab crushes Baron Telyx V`Zher for 13 points of damage."
        # "A grikbar kobold hits YOU for 2 points of damage. (Riposte)"
        (re.compile(
            r"^(?P<source>.+?) (?P<verb>" + MELEE_VERB_ALT + r") (?P<target>.+?) for "
            r"(?P<amount>\d+) points? of damage\."
            r"(?: \((?P<tag>Critical|Riposte|Flurry|Strikethrough)\))?$"
        ), h_melee_other),

        # "You try to pierce a grikbar kobold, but miss!"
        (re.compile(r"^You try to (?P<verb>\w+) (?P<target>.+?), but miss!$"), h_miss_self),

        # "You try to pierce a greater kobold, but a greater kobold dodges!" / "...parries!"
        (re.compile(
            r"^You try to (?P<verb>\w+) (?P<target>.+?), but .+? "
            r"(?P<outcome>" + DODGE_PARRY_ALT + r")!$"
        ), h_miss_self_avoided),

        # "A grikbar kobold tries to hit YOU, but misses!" (occasionally
        # tagged "(Riposte)"/"(Flurry)" -- doesn't change the outcome, just
        # tolerated so the line still parses). Verb list is explicit rather
        # than \w+ so a compound verb like "frenzy on" doesn't get its "on"
        # mis-split into the target.
        (re.compile(
            r"^(?P<source>.+?) tries to (?P<verb>" + INCOMING_ATTACK_VERB_ALT + r") "
            r"(?P<target>.+?), but misses!(?: \((?:Riposte|Flurry)\))?$"
        ), h_miss_other),

        # "A grikbar kobold tries to hit YOU, but YOU dodge!" / "parry!" / "riposte!" / "block!"
        (re.compile(
            r"^(?P<source>.+?) tries to (?P<verb>" + INCOMING_ATTACK_VERB_ALT + r") YOU, but YOU "
            r"(?P<outcome>dodge|parry|riposte|block)!$"
        ), h_evade_incoming),

        # "You healed Cheerful for 349 hit points by Greater Healing."
        # "You healed Cheerful for 5 (10) hit points by Blood Siphon Strike."
        (re.compile(
            r"^You healed (?P<target>.+?) for (?P<amount>\d+)(?: \((?P<potential>\d+)\))? "
            r"hit points? by (?P<spell>.+?)\.$"
        ), h_heal),

        # "Auraline healed Cheerful for 108 (457) hit points by Spirit Salve."
        (re.compile(
            r"^(?P<source>.+?) healed (?P<target>.+?) for (?P<amount>\d+)(?: \((?P<potential>\d+)\))? "
            r"hit points? by (?P<spell>.+?)\.$"
        ), h_heal_other),

        # "A grikbar kobold has taken 10 damage from your Blood Siphon Strike."
        (re.compile(
            r"^(?P<target>.+?) has taken (?P<amount>\d+) damage from your (?P<spell>.+?)\.$"
        ), h_taken_from_your),

        # "A grikbar kobold has taken 10 damage from Jasarn's Blood Siphon Strike."
        (re.compile(
            r"^(?P<target>.+?) has taken (?P<amount>\d+) damage from (?P<source>.+?)'s (?P<spell>.+?)\.$"
        ), h_taken_from_others),

        # "You have taken 6 damage from Flame Lick by orc taskmaster." -- same
        # event as above, reversed word order, always targeting you.
        (re.compile(
            r"^You have taken (?P<amount>\d+) damage from (?P<spell>.+?) by (?P<source>.+?)\.$"
        ), h_taken_from_by),

        # "YOU are pierced by a gloomwater mermaid's thorns for 10 points of non-melee damage!"
        (re.compile(
            r"^YOU are (?P<verb>\w+) by (?P<source>.+?) for (?P<amount>\d+) points? of "
            r"non-melee damage!?$"
        ), h_nonmelee_you),

        # "The goblin sage is burned by YOUR flames for 10 points of non-melee damage."
        (re.compile(
            r"^(?P<target>.+?) is (?P<verb>\w+) by (?P<source>.+?) for (?P<amount>\d+) points? "
            r"of non-melee damage\.?!?$"
        ), h_nonmelee_other),

        # "You begin casting Greater Healing." / "A Teir`Dal ranger begins casting Shield of Thistles."
        (re.compile(r"^(?P<source>.+?) begins? casting (?P<spell>.+?)\.$"), h_cast_begin),

        (re.compile(r"^You have slain (?P<target>.+?)!$"), h_slain_self),
        (re.compile(r"^(?P<source>.+?) has slain (?P<target>.+?)!$"), h_slain_other),
        (re.compile(r"^(?P<target>.+?) has been slain by (?P<source>.+?)!$"), h_slain_by),
        (re.compile(r"^You died\.$"), h_you_died),
        (re.compile(r"^You escape from combat\.$"), h_escape),

        # "A grikbar kobold resisted your Stun!" (format expected, not yet observed in sample)
        (re.compile(r"^(?P<target>.+?) resisted your (?P<spell>.+?)!?$"), h_resisted),

        # "a greater kobold is struck by a sudden force." -- generic no-amount spell effect
        (re.compile(r"^(?P<target>.+?) is struck by (?P<desc>.+?)\.$"), h_struck_by_generic),

        (re.compile(r"^You have entered (?P<zone>.+?)\.$"), h_zone_change),

        (re.compile(r"^You gain experience! \((?P<pct>[\d.]+)%\)$"), h_exp),

        (re.compile(r"^You receive (?P<text>.+) from the corpse\.$"), h_currency),

        # "You looted a Malachite from a grikbar kobold's corpse ..."
        # "You looted 2 Zombie Skin from a tormented dead's corpse ..."
        (re.compile(
            r"^You looted (?:(?P<qty>\d+) )?(?P<item>.+?) from (?P<source>.+?)'s corpse"
        ), h_loot),

        (re.compile(
            r"^Your faction standing with (?P<faction>.+?) has been adjusted by (?P<delta>-?\d+)\.$"
        ), h_faction),
    ]
