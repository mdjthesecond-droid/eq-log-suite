import re
from datetime import datetime
from typing import Callable, Optional

from eq_log_suite.models import ParsedEvent

class GameParser:
    """Base class for a per-game log parser.

    Subclasses set `game_code` and `PATTERNS`: an ordered list of
    (compiled_regex, handler) pairs tried against the text following the
    leading timestamp. The first match wins; `handler(match)` returns a
    ParsedEvent with ts/raw_line/line_no left unset (parse_line fills them
    in), or None to fall through to the next pattern.

    Subclasses may override TIMESTAMP_RE/TIMESTAMP_FMT if the game's log
    lines don't use classic EQ's `[Day Mon DD HH:MM:SS YYYY] rest` format --
    TIMESTAMP_RE just needs named groups `ts` (parsed with TIMESTAMP_FMT) and
    `rest` (everything PATTERNS match against).
    """

    game_code: str = ""
    TIMESTAMP_RE: re.Pattern = re.compile(
        r"^\[(?P<ts>[A-Za-z]{3} [A-Za-z]{3} +\d{1,2} \d{2}:\d{2}:\d{2} \d{4})\] (?P<rest>.*)$"
    )
    TIMESTAMP_FMT: str = "%a %b %d %H:%M:%S %Y"
    PATTERNS: list[tuple[re.Pattern, Callable[[re.Match], Optional[ParsedEvent]]]] = []

    @classmethod
    def parse_timestamp(cls, line: str) -> Optional[tuple[datetime, str]]:
        m = cls.TIMESTAMP_RE.match(line)
        if not m:
            return None
        try:
            ts = datetime.strptime(m.group("ts"), cls.TIMESTAMP_FMT)
        except ValueError:
            return None
        return ts, m.group("rest")

    @classmethod
    def parse_line(cls, line: str, line_no: int = 0) -> Optional[ParsedEvent]:
        # EQ log files are written with Windows CRLF line endings (the
        # client is a Windows binary even under Proton) -- a stray \r left
        # on the end of `line` would break every pattern's `$` end-anchor.
        line = line.rstrip("\r\n")
        parsed_ts = cls.parse_timestamp(line)
        if parsed_ts is None:
            return None
        ts, rest = parsed_ts
        for pattern, handler in cls.PATTERNS:
            m = pattern.match(rest)
            if not m:
                continue
            event = handler(m)
            if event is None:
                continue
            event.ts = ts
            event.raw_line = line
            event.line_no = line_no
            return event
        return None

    @classmethod
    def classify_actor(cls, name: str, self_name: Optional[str] = None) -> str:
        """Best-effort actor typing from text alone: 'you', 'npc' (generic,
        article-prefixed trash mob name), or 'unknown' (named/proper-noun
        actor -- could be a player, pet, or unique NPC; not distinguishable
        from log text alone)."""
        stripped = name.strip()
        if stripped.upper() == "YOU" or stripped.lower() == "you":
            return "you"
        if self_name and stripped.lower() == self_name.lower():
            return "you"
        if re.match(r"^(a|an|the)\s", stripped, re.IGNORECASE):
            return "npc"
        return "unknown"


# The client capitalizes whatever word starts a log line's sentence -- for a
# generic trash mob that means "A jeering gargoyle hits YOU..." (mob dealing
# damage, capitalized) vs "You hit a jeering gargoyle..." (mob taking damage,
# lowercase mid-sentence). Same mob either way; normalize the leading article
# back to lowercase so every downstream query groups them as one NPC instead
# of two. No lowercase-next-letter requirement -- confirmed against real data
# that this also applies to names with a capitalized second word (e.g. "A
# Foul Wind" / "a Foul Wind", "A Teir`Dal priest" / "a Teir`Dal priest"), only
# the leading article's own case ever changes. Deliberately narrow (only a
# bare "A "/"An " prefix, not "The " -- that one's more likely a genuine
# proper-noun unique name) and confirmed empirically: every capitalized-
# article name in the existing dataset already had an exact lowercase twin,
# none were capitalized-only.
_LEADING_ARTICLE_RE = re.compile(r"^(A|An) ")


def normalize_actor_name(name: Optional[str]) -> Optional[str]:
    if not name:
        return name
    return _LEADING_ARTICLE_RE.sub(lambda m: m.group(1).lower() + " ", name)


# Item names, unlike actor names, are looted with a grammatical article that
# isn't part of the item itself ("You looted a Malachite...", "You loot
# \aITEM...:a poison gland\/a..."), so it should be dropped rather than
# case-normalized. Deliberately only strips a lowercase "a "/"an " -- a
# capitalized "A "/"An " is a real proper-noun item name (confirmed real:
# "A Foul Wind", an EQ2 loot drop), same rationale as _LEADING_ARTICLE_RE
# above but the opposite conclusion for items vs. actors.
_LEADING_ITEM_ARTICLE_RE = re.compile(r"^(?:a|an) ")


def strip_item_article(name: Optional[str]) -> Optional[str]:
    if not name:
        return name
    return _LEADING_ITEM_ARTICLE_RE.sub("", name)
