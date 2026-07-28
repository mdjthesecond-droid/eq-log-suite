"""Parser for classic/live EverQuest logs. Same log grammar as EQ Legends
(EQL) -- both are EQ-client-derived -- so this starts as a plain subclass
reusing EQLegendsParser's PATTERNS wholesale rather than a hand-copied
duplicate. Diverge here (override PATTERNS/handlers) once real EQ logs
surface differences; per EQL's own docstring, don't guess -- confirm against
a real log first.

Two known differences already called out, neither of which needs a parser
change: EQ has no EQL-style instanced-zone difficulty tiers (no trailing
"<zone> N (Label)" suffix, see parse_zone_tier() in eq_legends.py -- zone
names simply won't match that pattern, so tier stays 0 for everything), and
no EQL-style "+1..+10" item enhancement suffixes (there was never any such
parsing here to begin with).

One confirmed real difference so far (from a live Gloomingdeep-tutorial
sample, eqlog_Cheerfulish_povar.txt): loot lines are bracketed in dashes and
say "have looted", not EQL's bare "You looted" --
"--You have looted a The Gloomingdeep Jailor's Key from The Gloomingdeep
Jailor's corpse.--" vs EQL's "You looted a Malachite from a grikbar
kobold's corpse ...". Combat (melee/miss/heal), /con, slain, and "You gain
experience!" lines all matched EQL's existing patterns as-is in that same
sample.

A second loot variant confirmed in that same sample: lootable objects/nodes
(not corpses) -- "--You have looted a Rat Ears from a vermin nest .--",
"...from a kobold barrel .--", "...from a hollows mushroom .--" -- drop the
"'s corpse" wording entirely, just "from <source> .--" with a trailing
space. _EQ_LOOT_OBJECT_RE below handles that form separately since
_EQ_LOOT_RE's "'s corpse" is required text, not optional.

Live EQ also has a structured "Tasks" system -- unlike EQL, which (per its
own docstring) has no logged quest-completion signal at all, just branching
NPC dialogue. Confirmed real task lines from that same sample:
  "You have been assigned the task 'Jail Break!'."
  "Your task 'Jail Break!' has been updated."                (x many, no detail)
  "You've completed the tutorial on Hotbars!"                (tutorial-flavored completion)
  "You have completed achievement: Mastering Achievements"   (achievement-flavored completion)
  "You have successfully been granted your reward for: Hotbars"
task_assigned and task_reward are parsed below -- the two completion-
announcement phrasings above are confirmed for tutorial/achievement-style
tasks specifically, not for a plain task (no example of that wording seen
yet), and the reward-grant line is the one that's fired for every completion
observed so far regardless of task flavor, so it doubles as "this task is
done" without guessing at unconfirmed wording. The assignment line never
names the NPC who gave it -- /tasks/eq correlates it the same way EQ2's
/quests correlates its own npc column, via whichever NPC most recently
spoke (npc_dialogue) before the assignment.

"...has been updated." is also parsed now (task_updated), despite firing
dozens of times per task with no detail of its own -- checked against a
real ~14k-line sample (eqlog_Cheerfulish_povar.txt) line by line: every
task_updated shares its exact (second-resolution) timestamp with either a
"You have slain <mob>!" (event_type death, source_name "You") or a
"--You have looted ...--" (event_type loot) line for that same character,
so /tasks/eq correlates each update to whichever of those shares its ts,
nearest by line_no if more than one candidate ties on ts. Two things this
sample ruled out, so don't assume otherwise without a fresh confirmed
sample: task_updated does NOT come strictly after its trigger line in file
order (it's interleaved mid-burst -- e.g. slash-for-damage, then
task_updated, then "You gain experience!", then the corpse-falls line,
then "You have slain ...!", all same second), and looting a stack of N
does NOT produce N task_updated lines -- every "--You have looted 2/3/4/5
...--" line seen only ever paired with exactly one task_updated, same as a
qty-1 loot.

Also seen in that sample but not yet parsed into an event (harmless --
falls through to raw_lines, not lost): "You have gained a level! Welcome to
level N!".
"""

import re

from eq_log_suite.models import ParsedEvent
from eq_log_suite.parsers.eq_legends import EQLegendsParser, h_loot

# "--You have looted a The Gloomingdeep Jailor's Key from The Gloomingdeep
# Jailor's corpse.--" -- confirmed real (1/1 sample so far). Optional qty
# digit kept for parity with EQL's own h_loot, not yet observed here.
_EQ_LOOT_RE = re.compile(
    r"^--You have looted (?:(?P<qty>\d+) )?(?P<item>.+?) from (?P<source>.+?)'s corpse\.--$"
)

# Same bracketing, but for a lootable object/node rather than a corpse --
# confirmed real: "--You have looted a Rat Ears from a vermin nest .--",
# "...from a kobold barrel .--", "...from a hollows mushroom .--" (all from
# the same Cheerfulish sample). No "'s corpse" here, just a trailing space
# before the period -- needs its own pattern since _EQ_LOOT_RE's "'s corpse"
# is required, not optional.
_EQ_LOOT_OBJECT_RE = re.compile(
    r"^--You have looted (?:(?P<qty>\d+) )?(?P<item>.+?) from (?P<source>.+?) \.--$"
)


def h_task_assigned(m):
    return ParsedEvent(
        ts=None, raw_line=None, event_type="task_assigned",
        source_name=None, source_type=None,
        target_name=m.group("task"), target_type="task",
        verb=None, amount=None, outcome=None,
    )


def h_task_reward(m):
    return ParsedEvent(
        ts=None, raw_line=None, event_type="task_reward",
        source_name=None, source_type=None,
        target_name=m.group("task"), target_type="task",
        verb=None, amount=None, outcome=None,
    )


def h_task_updated(m):
    return ParsedEvent(
        ts=None, raw_line=None, event_type="task_updated",
        source_name=None, source_type=None,
        target_name=m.group("task"), target_type="task",
        verb=None, amount=None, outcome=None,
    )


class EQParser(EQLegendsParser):
    game_code = "eq"

    PATTERNS = [
        (_EQ_LOOT_RE, h_loot),
        (_EQ_LOOT_OBJECT_RE, h_loot),
        (re.compile(r"^You have been assigned the task '(?P<task>.+)'\.$"), h_task_assigned),
        (re.compile(r"^You have successfully been granted your reward for: (?P<task>.+)$"), h_task_reward),
        (re.compile(r"^Your task '(?P<task>.+)' has been updated\.$"), h_task_updated),
    ] + EQLegendsParser.PATTERNS
