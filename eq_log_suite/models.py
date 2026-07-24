from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass
class ParsedEvent:
    """One decomposed combat/game event, produced by a GameParser and
    written to the `events` table. `extra` carries anything game-specific
    that doesn't warrant its own column."""

    ts: datetime
    event_type: str
    raw_line: str
    line_no: int = 0
    source_name: Optional[str] = None
    source_type: Optional[str] = None
    target_name: Optional[str] = None
    target_type: Optional[str] = None
    verb: Optional[str] = None
    amount: Optional[int] = None
    outcome: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)
