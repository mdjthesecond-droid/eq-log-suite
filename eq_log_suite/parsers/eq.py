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

Live EQ also has a structured "Tasks" system -- unlike EQL, which (per its
own docstring) has no logged quest-completion signal at all, just branching
NPC dialogue. Confirmed real task lines from that same sample:
  "You have been assigned the task 'Jail Break!'."
  "Your task 'Jail Break!' has been updated."                (x many, no detail)
  "You've completed the tutorial on Hotbars!"                (tutorial-flavored completion)
  "You have completed achievement: Mastering Achievements"   (achievement-flavored completion)
  "You have successfully been granted your reward for: Hotbars"
Only task_assigned and task_reward are parsed below -- the two completion-
announcement phrasings above are confirmed for tutorial/achievement-style
tasks specifically, not for a plain task (no example of that wording seen
yet), and the reward-grant line is the one that's fired for every completion
observed so far regardless of task flavor, so it doubles as "this task is
done" without guessing at unconfirmed wording. "...has been updated." is
pure noise (fires dozens of times per task, no new information each time)
and is deliberately left unparsed. The assignment line never names the
NPC who gave it -- /tasks/eq correlates it the same way EQ2's /quests
correlates its own npc column, via whichever NPC most recently spoke
(npc_dialogue) before the assignment.

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


class EQParser(EQLegendsParser):
    game_code = "eq"

    PATTERNS = [
        (_EQ_LOOT_RE, h_loot),
        (re.compile(r"^You have been assigned the task '(?P<task>.+)'\.$"), h_task_assigned),
        (re.compile(r"^You have successfully been granted your reward for: (?P<task>.+)$"), h_task_reward),
    ] + EQLegendsParser.PATTERNS
