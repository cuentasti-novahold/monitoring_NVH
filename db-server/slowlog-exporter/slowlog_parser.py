"""Stateful parser for the MySQL 8.0 slow query log (FILE output).

The parser is pure: it has no I/O and no Prometheus dependency, so it can be
unit tested in isolation and reused by the exporter.

Why it must be stateful
-----------------------
MySQL writes a ``use <db>;`` line only when the default database of the
statement differs from the last database written to the log file. That
"last database" is global state of the log writer, not per connection. So a
consumer of the log must remember the last ``use`` and apply it to every
following entry until the next ``use`` line appears.

Known limitations (documented in GUIA.md):
* A connection without a default database writes no ``use`` line, so its
  entries inherit the previous schema.
* The schema is the connection's default database, not the tables read.
  ``SELECT * FROM FINANCE.payments`` issued while connected to ``DW`` is
  attributed to ``DW``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

UNKNOWN_SCHEMA = "(none)"

_TIME_RE = re.compile(r"^# Time: ")
_USER_HOST_RE = re.compile(
    r"^# User@Host: (?P<priv>[^\[]*)\[(?P<user>[^\]]*)\]\s*@\s*(?P<host>[^\s\[]*)\s*\[(?P<ip>[^\]]*)\]"
)
_KV_RE = re.compile(r"(\w+): (\S+)")
_USE_RE = re.compile(r"^use (?P<db>.+);$")
_SET_TIMESTAMP_RE = re.compile(r"^SET timestamp=\d+;$")
_HEADER_RES = (
    re.compile(r"^\S*mysqld(\.exe)?, Version: "),
    re.compile(r"^Tcp port: "),
    re.compile(r"^Time\s+Id\s+Command\s+Argument"),
)


@dataclass
class SlowLogEntry:
    """One statement taken from the slow log."""

    user: str
    client_host: str
    client_ip: str
    schema: str
    query_time: Optional[float]
    rows_sent: Optional[int]
    rows_examined: Optional[int]
    bytes_sent: Optional[int]
    bytes_received: Optional[int]
    query: str


@dataclass
class _Pending:
    """Entry under construction."""

    schema: str
    user: Optional[str] = None
    client_host: str = ""
    client_ip: str = ""
    stats: dict = field(default_factory=dict)
    query_lines: list = field(default_factory=list)


def _to_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _to_float(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


class SlowLogParser:
    """Turns slow log lines into ``SlowLogEntry`` objects.

    Feed lines one at a time with ``feed``. An entry is returned once the
    next entry starts (a new ``# Time:`` line or a new ``# User@Host:`` line).
    Call ``flush`` to obtain the last pending entry.
    """

    def __init__(self) -> None:
        self.current_schema: str = UNKNOWN_SCHEMA
        self._pending: Optional[_Pending] = None
        # Incremented whenever a new entry begins; lets a caller remember the
        # file offset where the entry under construction started.
        self.entries_started = 0

    @property
    def has_pending(self) -> bool:
        """True while an entry has started (from its first line) but not been emitted."""
        return self._pending is not None

    # -- public API -------------------------------------------------------

    def feed(self, line: str) -> list[SlowLogEntry]:
        line = line.rstrip("\r\n")
        emitted: list[SlowLogEntry] = []

        if any(r.match(line) for r in _HEADER_RES):
            return emitted

        if _TIME_RE.match(line):
            emitted.extend(self.flush())
            self._pending = _Pending(schema=self.current_schema)
            self.entries_started += 1
            return emitted

        match = _USER_HOST_RE.match(line)
        if match:
            # A User@Host line starts a new entry unless it directly follows
            # the "# Time:" line of the entry under construction.
            if self._pending is None or self._pending.user is not None:
                emitted.extend(self.flush())
                self._pending = _Pending(schema=self.current_schema)
                self.entries_started += 1
            self._pending.user = match.group("user") or match.group("priv").strip()
            self._pending.client_host = match.group("host")
            self._pending.client_ip = match.group("ip")
            return emitted

        pending = self._pending
        if pending is None or pending.user is None:
            # Lines outside an entry (or before its User@Host) carry nothing useful.
            return emitted

        in_header = not pending.query_lines
        if in_header and line.startswith("# "):
            pending.stats.update(_KV_RE.findall(line))
            return emitted

        if in_header:
            use = _USE_RE.match(line)
            if use:
                self.current_schema = use.group("db").strip().strip("`")
                pending.schema = self.current_schema
                return emitted
            if _SET_TIMESTAMP_RE.match(line):
                return emitted

        pending.query_lines.append(line)
        return emitted

    def flush(self) -> list[SlowLogEntry]:
        pending, self._pending = self._pending, None
        if pending is None or pending.user is None:
            return []
        stats = pending.stats
        return [
            SlowLogEntry(
                user=pending.user,
                client_host=pending.client_host,
                client_ip=pending.client_ip,
                schema=pending.schema,
                query_time=_to_float(stats.get("Query_time")),
                rows_sent=_to_int(stats.get("Rows_sent")),
                rows_examined=_to_int(stats.get("Rows_examined")),
                bytes_sent=_to_int(stats.get("Bytes_sent")),
                bytes_received=_to_int(stats.get("Bytes_received")),
                query="\n".join(pending.query_lines).strip(),
            )
        ]
