from pathlib import Path
from typing import Optional

from eq_log_suite.parsers.base import GameParser
from eq_log_suite.parsers.eq2 import EQ2Parser
from eq_log_suite.parsers.eq_legends import EQLegendsParser

PARSERS: dict[str, type[GameParser]] = {
    EQLegendsParser.game_code: EQLegendsParser,
    EQ2Parser.game_code: EQ2Parser,
}


def get_parser(game_code: str) -> type[GameParser]:
    try:
        return PARSERS[game_code]
    except KeyError:
        raise ValueError(
            f"unknown game code {game_code!r}; known games: {sorted(PARSERS)}"
        )


def detect_parser(path: str, sample_lines: int = 200) -> Optional[type[GameParser]]:
    """Sniff which game a log file belongs to by scoring how many of its
    first `sample_lines` each registered parser can decompose into an
    event. Ties favor whichever parser is listed first in PARSERS."""
    lines = []
    with open(path, errors="replace") as f:
        for _ in range(sample_lines):
            line = f.readline()
            if not line:
                break
            lines.append(line)

    best_parser, best_score = None, -1
    for parser in PARSERS.values():
        score = sum(1 for line in lines if parser.parse_line(line) is not None)
        if score > best_score:
            best_parser, best_score = parser, score
    return best_parser if best_score > 0 else None
