"""Prometheus exporter that tails the MySQL slow log and counts bytes.

It follows the slow log file continuously (like ``tail -F``), parses every
entry with ``SlowLogParser`` and exposes counters labelled by schema, MySQL
user and client IP on ``http://<listen-address>:<port>/metrics`` (default
``0.0.0.0``; local mode on Windows uses ``127.0.0.1`` so that no firewall
prompt appears).

Startup behaviour:
- with ``--state-file`` and a saved position for the same file, it resumes
  exactly there, so entries written while the exporter was down are counted
  (an entry that was half processed is re-read and counted once);
- if the file was rotated while it was down, the new file is read from the start;
- otherwise, without ``--from-beginning``, the existing content up to the size
  seen when opening is read only to learn the ``use <db>;`` schema state (no
  metrics); anything appended during that scan is counted. With
  ``--from-beginning`` the existing content is also counted.
The position is saved every second and on shutdown; after a crash (not a clean
stop) the entries of that last second may be counted twice. If the state file
cannot be written, counting continues and a warning is logged once.

Rotation: logrotate renames the file and creates the new one before
``FLUSH SLOW LOGS``, so MySQL keeps writing to the old inode for a moment.
When the inode of the path changes, the exporter keeps reading the old handle
until it has been quiet for ROTATION_GRACE_SECONDS, then reads the new file
from the beginning. If the path disappears, it logs a warning and keeps
reading the open handle. If the file is truncated in place, it seeks back to
the start.

Counters are in-process: they restart from zero when the exporter restarts.
Prometheus ``rate()``/``increase()`` handle such counter resets.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import time
from typing import Callable, Optional, TextIO

from prometheus_client import Counter, start_http_server

from slowlog_parser import SlowLogEntry, SlowLogParser

LABELS = ("schema", "user", "client_ip")

BYTES_SENT = Counter(
    "mysql_slowlog_bytes_sent", "Bytes sent to clients (slow log Bytes_sent).", LABELS
)
BYTES_RECEIVED = Counter(
    "mysql_slowlog_bytes_received",
    "Bytes received from clients (slow log Bytes_received).",
    LABELS,
)
ROWS_SENT = Counter("mysql_slowlog_rows_sent", "Rows sent to clients.", LABELS)
QUERIES = Counter("mysql_slowlog_queries", "Statements seen in the slow log.", LABELS)
NO_BYTES = Counter(
    "mysql_slowlog_entries_without_bytes",
    "Entries without Bytes_sent (log_slow_extra is probably OFF).",
)
PARSE_ERRORS = Counter(
    "mysql_slowlog_parse_errors", "Entries that could not be parsed completely."
)
STATE_SAVE_ERRORS = Counter(
    "mysql_slowlog_state_save_errors",
    "Failed writes of the --state-file resume position (counting continues).",
)

POLL_SECONDS = 0.5
# An entry is emitted when the next one starts. If the log goes quiet, the
# last entry is flushed after this many idle seconds.
IDLE_FLUSH_SECONDS = 5.0
# After a rotation, the old file must stay quiet this long before switching.
ROTATION_GRACE_SECONDS = 10.0
# How often the resume position is written to --state-file. After a clean stop
# nothing is counted twice; after a crash (SIGKILL, OOM) entries processed in
# the last interval are counted again on restart, so keep it short.
STATE_SAVE_SECONDS = 1.0

log = logging.getLogger("slowlog_exporter")


def client_label(entry: SlowLogEntry) -> str:
    """Value of the client_ip label.

    Connections over the local socket have no IP; their host name is prefixed
    with ``host:`` so it can never be mistaken for an address in PromQL filters.
    """
    if entry.client_ip:
        return entry.client_ip
    return f"host:{entry.client_host}" if entry.client_host else "unknown"


class Exporter:
    def __init__(
        self,
        path: str,
        exclude_users: set[str],
        state_file: Optional[str] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.path = path
        self.exclude_users = exclude_users
        self.state_file = state_file
        self.clock = clock
        self.parser = SlowLogParser()
        self.handle: Optional[TextIO] = None
        self.inode: Optional[int] = None
        self.partial = ""
        self.running = True
        # Resume bookkeeping: file offset where the line being assembled began,
        # and where (and with which schema) the entry under construction began.
        self.line_offset = 0
        self.entry_offset = 0
        self.entry_schema = self.parser.current_schema
        self.last_data = clock()
        self.last_save = clock()
        self.rotation_seen: Optional[float] = None
        self.missing_logged = False
        self.state_error_logged = False

    # -- metrics ------------------------------------------------------------

    def record(self, entry: SlowLogEntry) -> None:
        if entry.user in self.exclude_users:
            return
        if entry.query_time is None:
            PARSE_ERRORS.inc()
            return
        labels = (entry.schema, entry.user, client_label(entry))
        QUERIES.labels(*labels).inc()
        if entry.rows_sent is not None:
            ROWS_SENT.labels(*labels).inc(entry.rows_sent)
        if entry.bytes_sent is None:
            NO_BYTES.inc()
            return
        BYTES_SENT.labels(*labels).inc(entry.bytes_sent)
        if entry.bytes_received is not None:
            BYTES_RECEIVED.labels(*labels).inc(entry.bytes_received)

    def process_line(self, line: str, emit: bool = True) -> None:
        started = self.parser.entries_started
        try:
            entries = self.parser.feed(line)
        except Exception:  # defensive: never let one bad line stop the exporter
            log.exception("failed to parse line")
            PARSE_ERRORS.inc()
            return
        if self.parser.entries_started != started:
            # This line opened a new entry: a restart must resume from here.
            self.entry_offset = self.line_offset
            self.entry_schema = self.parser.current_schema
        if emit:
            for entry in entries:
                self.record(entry)

    def flush(self, emit: bool = True) -> None:
        for entry in self.parser.flush():
            if emit:
                self.record(entry)

    # -- resume state -----------------------------------------------------------

    def save_state(self) -> None:
        """Persist where a restart must resume (atomic write)."""
        if not self.state_file or self.handle is None:
            return
        if self.parser.has_pending:
            # The pending entry is not counted yet: resume at its first line.
            offset, schema = self.entry_offset, self.entry_schema
        else:
            offset, schema = self.line_offset, self.parser.current_schema
        state = {"inode": self.inode, "offset": offset, "schema": schema}
        tmp = self.state_file + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(state, fh)
            os.replace(tmp, self.state_file)
        except OSError as exc:
            # A full or read-only state volume must not stop the counting:
            # only the ability to resume after a restart is degraded.
            STATE_SAVE_ERRORS.inc()
            if not self.state_error_logged:
                log.warning("could not save state to %s: %s (resume after restart is degraded)", self.state_file, exc)
                self.state_error_logged = True
            return
        if self.state_error_logged:
            log.info("state saved again to %s", self.state_file)
            self.state_error_logged = False

    def load_state(self) -> Optional[dict]:
        if not self.state_file:
            return None
        try:
            with open(self.state_file, encoding="utf-8") as fh:
                state = json.load(fh)
            return {
                "inode": int(state["inode"]),
                "offset": int(state["offset"]),
                "schema": str(state["schema"]),
            }
        except FileNotFoundError:
            return None
        except (ValueError, KeyError, TypeError):
            log.warning("ignoring unreadable state file %s", self.state_file)
            return None

    # -- file following -------------------------------------------------------

    def _snapshot_size(self) -> int:
        return os.fstat(self.handle.fileno()).st_size

    def open(self, from_beginning: bool, resume: bool = True) -> bool:
        try:
            self.handle = open(self.path, "r", encoding="utf-8", errors="replace", newline="")
        except FileNotFoundError:
            return False
        self.inode = os.fstat(self.handle.fileno()).st_ino
        self.partial = ""
        size = self._snapshot_size()
        state = self.load_state() if resume else None

        if state and state["inode"] == self.inode and state["offset"] <= size:
            self.handle.seek(state["offset"])
            self.parser.current_schema = state["schema"]
            log.info("resuming %s at offset %d (schema %s)", self.path, state["offset"], state["schema"])
        elif state and state["inode"] != self.inode:
            # Rotated while the exporter was down: everything in this file is new.
            log.warning("%s was rotated while the exporter was down; reading it from the start", self.path)
        elif not from_beginning:
            self._learn_history(size)
        self.line_offset = self.handle.tell()
        self.entry_offset = self.line_offset
        self.entry_schema = self.parser.current_schema
        self.last_data = self.clock()
        log.info("following %s (inode %s)", self.path, self.inode)
        return True

    def _learn_history(self, size: int) -> None:
        """Read complete lines up to ``size`` only to learn the schema state.

        Stopping at the size seen when opening means entries MySQL appends
        during this scan are left for the follower, which counts them.
        """
        while self.handle.tell() < size:
            start = self.handle.tell()
            line = self.handle.readline()
            if not line.endswith("\n"):
                self.handle.seek(start)  # partial last line: the follower finishes it
                break
            self.process_line(line, emit=False)
        self.flush(emit=False)

    def read_available(self) -> bool:
        """Read all complete lines currently available. Returns True if any."""
        got_data = False
        while True:
            if not self.partial:
                self.line_offset = self.handle.tell()
            chunk = self.handle.readline()
            if not chunk:
                return got_data
            got_data = True
            self.partial += chunk
            if self.partial.endswith("\n"):
                line, self.partial = self.partial, ""
                self.process_line(line)

    def rotated_or_truncated(self) -> str:
        try:
            stat = os.stat(self.path)
        except FileNotFoundError:
            return "missing"
        if stat.st_ino != self.inode:
            return "rotated"
        if stat.st_size < self.handle.tell():
            return "truncated"
        return ""

    def step(self) -> None:
        """One polling iteration: read, flush when idle, handle rotation, save state."""
        now = self.clock()
        if self.read_available():
            self.last_data = now
        elif now - self.last_data > IDLE_FLUSH_SECONDS:
            self.flush()

        state = self.rotated_or_truncated()
        if state == "rotated":
            self._follow_rotation(now)
        elif state == "truncated":
            log.info("log truncated, seeking to start")
            self.handle.seek(0)
            self.partial = ""
        elif state == "missing":
            if not self.missing_logged:
                log.warning("%s is missing; still reading the open file until it reappears", self.path)
                self.missing_logged = True
        else:
            self.missing_logged = False

        if self.state_file and now - self.last_save >= STATE_SAVE_SECONDS:
            self.save_state()
            self.last_save = now

    def _follow_rotation(self, now: float) -> None:
        # logrotate renames the file and creates the new one before
        # `flush-logs slow`: MySQL keeps writing to the old inode until then.
        # Keep reading the old handle until it has been quiet for a grace period.
        if self.rotation_seen is None:
            self.rotation_seen = now
            log.info("log rotated; draining the old file until it is quiet")
        quiet_since = max(self.last_data, self.rotation_seen)
        if now - quiet_since < ROTATION_GRACE_SECONDS:
            return
        self.read_available()
        self.flush()
        self.handle.close()
        self.rotation_seen = None
        # The new file is read from the beginning: every line in it is new.
        while self.running and not self.open(from_beginning=True, resume=False):
            time.sleep(1)
        log.info("switched to the new log file")

    def run(self, from_beginning: bool) -> None:
        while self.running and not self.open(from_beginning):
            log.warning("waiting for %s to appear", self.path)
            time.sleep(5)
        while self.running:
            self.step()
            time.sleep(POLL_SECONDS)
        self.save_state()

    def stop(self, *_args) -> None:
        self.running = False


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--log-file", default="/var/log/mysql/mysql-slow.log")
    ap.add_argument("--port", type=int, default=9105)
    ap.add_argument("--listen-address", default="0.0.0.0", help="Address of the /metrics endpoint.")
    ap.add_argument("--from-beginning", action="store_true")
    ap.add_argument(
        "--state-file",
        default=None,
        help="File where the read position is saved, so a restart resumes without losing entries.",
    )
    ap.add_argument(
        "--exclude-users",
        default="exporter,root,debian-sys-maint",
        help="Comma separated MySQL users ignored by the metrics (monitoring noise).",
    )
    return ap.parse_args(argv)


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    exclude = {u.strip() for u in args.exclude_users.split(",") if u.strip()}
    exporter = Exporter(args.log_file, exclude, state_file=args.state_file)
    signal.signal(signal.SIGTERM, exporter.stop)
    signal.signal(signal.SIGINT, exporter.stop)
    start_http_server(args.port, addr=args.listen_address)
    log.info("metrics on %s:%d, excluding users %s", args.listen_address, args.port, sorted(exclude))
    exporter.run(args.from_beginning)


if __name__ == "__main__":
    main()
