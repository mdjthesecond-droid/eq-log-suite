"""Parser for EverQuest II logs.

Derived from a real live sample: /home/myself/eq2/logs/Halls of Fate/testers.txt
EQ2's format differs from classic EQ in two ways:
  - each line is prefixed with a raw unix-epoch timestamp in parens before
    the usual bracketed one, e.g. "(1784811929)[Thu Jul 23 09:05:29 2026] ..."
  - combat phrasing puts the damage type before the word "damage" and names
    the ability as "YOUR <Ability> hits <target> for N <type> damage."
    instead of classic EQ's "You <verb> <target> for N points of damage."
Coverage (from the sample seen so far): named-ability hits, plain autoattack
hits, misses (self and incoming), kills, XP gain (including encounter-kill
bonus XP), AA experience conversion, combat start/stop, tradeskill gathering
(mining/foresting/gathering/trapping, including rare-item tagging via
ingest.RareGatherTagger), and zone changes (same "You have entered X."
phrasing as classic EQ). Anything else (chat, loot from mob corpses, etc.)
just isn't decomposed yet -- extend PATTERNS as new shapes show up in
raw_lines.
"""

import re

from eq_log_suite.models import ParsedEvent
from eq_log_suite.parsers.base import GameParser


def h_ability_hit_self(m):
    return ParsedEvent(
        ts=None, raw_line=None, event_type="ability_damage",
        source_name="You", source_type="you",
        target_name=m.group("target"), target_type=GameParser.classify_actor(m.group("target")),
        verb=m.group("spell"), amount=int(m.group("amount")), outcome="hit",
        extra={"damage_type": m.group("dmgtype")},
    )


def h_autoattack_self(m):
    return ParsedEvent(
        ts=None, raw_line=None, event_type="melee",
        source_name="You", source_type="you",
        target_name=m.group("target"), target_type=GameParser.classify_actor(m.group("target")),
        verb=None, amount=int(m.group("amount")), outcome="hit",
        extra={"damage_type": m.group("dmgtype")},
    )


def h_hit_other(m):
    source = m.group("source")
    target = m.group("target")
    return ParsedEvent(
        ts=None, raw_line=None, event_type="melee",
        source_name=source, source_type=GameParser.classify_actor(source),
        target_name=target, target_type=GameParser.classify_actor(target),
        verb=None, amount=int(m.group("amount")), outcome="hit",
        extra={"damage_type": m.group("dmgtype")},
    )


def h_ability_hit_named_source(m):
    source = m.group("source")
    target = m.group("target")
    return ParsedEvent(
        ts=None, raw_line=None, event_type="ability_damage",
        source_name=source, source_type=GameParser.classify_actor(source),
        target_name=target, target_type=GameParser.classify_actor(target),
        verb=m.group("spell"), amount=int(m.group("amount")), outcome="hit",
        extra={"damage_type": m.group("dmgtype")},
    )


def h_miss_self(m):
    return ParsedEvent(
        ts=None, raw_line=None, event_type="melee",
        source_name="You", source_type="you",
        target_name=m.group("target"), target_type=GameParser.classify_actor(m.group("target")),
        verb=m.group("verb"), amount=None, outcome="miss",
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


_AVOID_OUTCOME = {
    "riposte": "riposte", "ripostes": "riposte",
    "parry": "parry", "parries": "parry",
    "dodge": "dodge", "dodges": "dodge",
    "block": "block", "blocks": "block",
}


def h_avoid_other(m):
    # "a moat rat tries to pierce YOU, but YOU riposte." (also parry/dodge/block)
    source = m.group("source")
    target = m.group("target")
    outcome_raw = m.group("outcome").lower()
    return ParsedEvent(
        ts=None, raw_line=None, event_type="melee",
        source_name=source, source_type=GameParser.classify_actor(source),
        target_name=target, target_type=GameParser.classify_actor(target),
        verb=m.group("verb"), amount=None, outcome=_AVOID_OUTCOME.get(outcome_raw, outcome_raw),
    )


def h_threat(m):
    direction = m.group("dir")
    return ParsedEvent(
        ts=None, raw_line=None, event_type="threat",
        source_name="You", source_type="you",
        target_name=m.group("target"), target_type=GameParser.classify_actor(m.group("target")),
        verb=m.group("spell"), amount=int(m.group("amount")), outcome=None,
        extra={"direction": "increase" if direction == "increases" else "decrease"},
    )


def h_combat_state(m):
    return ParsedEvent(
        ts=None, raw_line=None, event_type="combat_state",
        source_name="You", source_type="you",
        target_name=None, target_type=None,
        verb=m.group("state"), amount=None, outcome=None,
    )


def h_kill(m):
    return ParsedEvent(
        ts=None, raw_line=None, event_type="death",
        source_name="You", source_type="you",
        target_name=m.group("target"), target_type="npc",
        verb=None, amount=None, outcome=None,
    )


def h_gain_xp(m):
    bonus = m.group("bonus")
    return ParsedEvent(
        ts=None, raw_line=None, event_type="exp",
        source_name="You", source_type="you",
        target_name=None, target_type=None,
        verb=None, amount=int(m.group("base")), outcome=None,
        extra={
            "bonus": int(bonus) if bonus else 0,
            "encounter_bonus": m.group("encounter") is not None,
        },
    )


def h_convert_aa(m):
    return ParsedEvent(
        ts=None, raw_line=None, event_type="exp_convert",
        source_name="You", source_type="you",
        target_name=None, target_type=None,
        verb=None, amount=int(m.group("amount")), outcome=None,
        extra={"encounter_bonus": m.group("encounter") is not None},
    )


# Raw in-game verb -> tradeskill gathering skill name. Extend as new verbs
# show up in raw_lines (e.g. a "fished" verb for Fishing hasn't been
# observed yet). Unknown verbs fall back to a capitalized guess rather than
# failing to parse.
_GATHER_SKILL_MAP = {
    "mine": "Mining", "mined": "Mining",
    "gather": "Gathering", "gathered": "Gathering",
    "forest": "Foresting", "forested": "Foresting",
    "acquire": "Trapping", "acquired": "Trapping",  # creature-den/corpse-style nodes (pelts, meat, etc.)
    "fish": "Fishing", "fished": "Fishing",
}


def h_gather(m):
    verb_raw = m.group("verb")
    skill = _GATHER_SKILL_MAP.get(verb_raw, verb_raw.capitalize())
    return ParsedEvent(
        ts=None, raw_line=None, event_type="gather",
        source_name="You", source_type="you",
        target_name=m.group("item"), target_type="item",
        verb=skill, amount=int(m.group("qty")), outcome=None,
        extra={"node": m.group("node"), "raw_verb": verb_raw},
    )


def h_rare_found(m):
    # Always immediately followed by the actual gather line in the log;
    # eq_log_suite.ingest.RareGatherTagger carries this forward and tags
    # that next 'gather' event's outcome, so "rare" is queryable directly
    # on the gather row itself instead of needing a manual line-adjacency
    # join later.
    return ParsedEvent(
        ts=None, raw_line=None, event_type="rare_found",
        source_name="You", source_type="you",
        target_name=None, target_type=None,
        verb=None, amount=None, outcome=m.group("tier"),
    )


def h_zone_change(m):
    return ParsedEvent(
        ts=None, raw_line=None, event_type="zone_change",
        source_name="You", source_type="you",
        target_name=m.group("zone"), target_type="zone",
        verb=None, amount=None, outcome=None,
    )


def h_mob_loot(m):
    source = m.group("source")
    return ParsedEvent(
        ts=None, raw_line=None, event_type="loot",
        source_name=source, source_type=GameParser.classify_actor(source),
        target_name=m.group("item"), target_type="item",
        verb=None, amount=1, outcome=None,
        extra={"via": "chest" if m.group("via").lower().startswith("the treasure") else "corpse"},
    )


def h_hail(m):
    return ParsedEvent(
        ts=None, raw_line=None, event_type="hail",
        source_name="You", source_type="you",
        target_name=m.group("npc"), target_type="npc",
        verb=None, amount=None, outcome=None,
    )


def h_quest_reward(m):
    return ParsedEvent(
        ts=None, raw_line=None, event_type="quest",
        source_name="You", source_type="you",
        target_name=m.group("quest"), target_type="quest",
        verb=None, amount=None, outcome=None,
        extra={"reward": m.group("reward")},
    )


class EQ2Parser(GameParser):
    game_code = "eq2"

    TIMESTAMP_RE = re.compile(
        r"^\((?P<epoch>\d+)\)\[(?P<ts>[A-Za-z]{3} [A-Za-z]{3} +\d{1,2} \d{2}:\d{2}:\d{2} \d{4})\] "
        r"(?P<rest>.*)$"
    )
    TIMESTAMP_FMT = "%a %b %d %H:%M:%S %Y"

    PATTERNS = [
        # "YOUR Viscerate hits a lowland viper for 99 piercing damage."
        (re.compile(
            r"^YOUR (?P<spell>.+?) hits (?P<target>.+?) for (?P<amount>\d+) (?P<dmgtype>\w+) damage\.$"
        ), h_ability_hit_self),

        # "YOU hit a lowland viper for 2 piercing damage." (plain autoattack)
        (re.compile(
            r"^YOU hit (?P<target>.+?) for (?P<amount>\d+) (?P<dmgtype>\w+) damage\.$"
        ), h_autoattack_self),

        # "<mob>'s <Ability> hits YOU for N type damage." (not yet observed; symmetric with h_ability_hit_self)
        (re.compile(
            r"^(?P<source>.+?)'s (?P<spell>.+?) hits (?P<target>.+?) for (?P<amount>\d+) (?P<dmgtype>\w+) damage\.$"
        ), h_ability_hit_named_source),

        # "a moat rat hits YOU for 5 piercing damage." (npc plain melee)
        (re.compile(
            r"^(?P<source>.+?) hits (?P<target>.+?) for (?P<amount>\d+) (?P<dmgtype>\w+) damage\.$"
        ), h_hit_other),

        # "YOU try to pierce a lowland viper, but miss."
        (re.compile(r"^YOU try to (?P<verb>\w+) (?P<target>.+?), but miss\.$"), h_miss_self),

        # "a lowland viper tries to pierce YOU, but misses."
        (re.compile(
            r"^(?P<source>.+?) tries to (?P<verb>\w+) (?P<target>.+?), but misses\.$"
        ), h_miss_other),

        # "a moat rat tries to pierce YOU, but YOU riposte." (also parry/dodge/block)
        (re.compile(
            r"^(?P<source>.+?) tries to (?P<verb>\w+) (?P<target>.+?), but YOU "
            r"(?P<outcome>ripostes?|parr(?:y|ies)|dodges?|blocks?)\.$"
        ), h_avoid_other),

        # "YOUR Evade reduces YOUR hate with a moat rat for 203 threat." (also "increases")
        (re.compile(
            r"^YOUR (?P<spell>.+?) (?P<dir>reduces|increases) YOUR hate with (?P<target>.+?) "
            r"for (?P<amount>\d+) threat\.$"
        ), h_threat),

        (re.compile(r"^You (?P<state>start|stop) fighting\.$"), h_combat_state),

        (re.compile(r"^You have killed (?P<target>.+?)\.$"), h_kill),

        # "You gain 68 XP plus 135 XP from bonuses!"
        # "You gain 14 XP for defeating the encounter plus 27 XP from bonuses!"
        (re.compile(
            r"^You gain (?P<base>\d+) XP(?P<encounter> for defeating the encounter)?"
            r"(?: plus (?P<bonus>\d+) XP from bonuses)?!$"
        ), h_gain_xp),

        # "You convert 101 experience into AA experience!"
        # "You convert 20 experience for defeating the encounter into AA experience!"
        (re.compile(
            r"^You convert (?P<amount>\d+) experience(?P<encounter> for defeating the encounter)? "
            r"into AA experience!$"
        ), h_convert_aa),

        # "You mined 2 \aITEM 782010437 526403903:solidified loam\/a from the murky ore."
        # Both present- and past-tense verb forms show up in practice (e.g.
        # "You mine ..." and "You mined ..." both occur) -- deliberately
        # excludes buy/created/receive/sell, which use the same \aITEM link
        # markup for merchant/crafting/quest-reward text, not a gather.
        (re.compile(
            r"^You (?P<verb>mine|mined|gather|gathered|forest|forested|acquire|acquired|fish|fished) "
            r"(?P<qty>\d+) \\aITEM -?\d+ -?\d+:(?P<item>[^\\]+)\\/a from (?P<node>.+?)\.$"
        ), h_gather),

        # "You have found a rare item!" (precedes the gather line above)
        (re.compile(r"^You have found an? (?P<tier>\w+) item!$"), h_rare_found),

        # "You have entered Antonica." (same phrasing as classic EQ)
        (re.compile(r"^You have entered (?P<zone>.+?)\.$"), h_zone_change),

        # "You loot \aITEM ...:Flash of Steel II (Adept)\/a from the Treasure Chest of a lowland viper."
        # "You loot \aITEM ...:a poison gland\/a from the corpse of a lowland viper."
        # Mob-kill loot -- distinct from tradeskill gathering (h_gather above),
        # which uses "You <mined/gathered/...> N \aITEM ... from <node>."
        (re.compile(
            r"^You loot \\aITEM -?\d+ -?\d+:(?P<item>[^\\]+)\\/a from "
            r"(?P<via>the Treasure Chest of|the corpse of) (?P<source>.+?)\.$"
        ), h_mob_loot),

        # "You receive 5 Silver, 82 Copper for completing Qeynosian Civil Service."
        (re.compile(r"^You receive (?P<reward>.+?) for completing (?P<quest>.+?)\.$"), h_quest_reward),

        # 'You say, "Hail, Knight-Lieutenant Alesso"' -- used to attribute a
        # quest completion to the NPC you most recently hailed, same idea as
        # zone correlation (see /quests).
        (re.compile(r'^You say, "Hail, (?P<npc>.+?)"$'), h_hail),
    ]
