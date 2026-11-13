"""Tests for the stateful MySQL slow log parser.

The fixtures mimic the real MySQL 8.0 slow log format with log_slow_extra=ON.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from slowlog_parser import SlowLogParser  # noqa: E402

EXTRA = (
    "Thread_id: 12 Errno: 0 Killed: 0 Bytes_received: {recv} Bytes_sent: {sent} "
    "Read_first: 1 Read_last: 0 Read_key: 1 Read_next: 0 Read_prev: 0 Read_rnd: 0 "
    "Read_rnd_next: 20001 Sort_merge_passes: 0 Sort_range_count: 0 Sort_rows: 0 "
    "Sort_scan_count: 0 Created_tmp_disk_tables: 0 Created_tmp_tables: 0 "
    "Start: 2026-10-16T12:00:00.000000Z End: 2026-10-16T12:00:00.512000Z"
)


def entry_lines(
    user="report_support",
    host="",
    ip="203.0.113.10",
    use=None,
    query="SELECT * FROM customers LIMIT 20000;",
    sent=10485760,
    recv=52,
    with_time=True,
    with_bytes=True,
):
    lines = []
    if with_time:
        lines.append("# Time: 2026-10-16T12:00:00.123456Z")
    lines.append(f"# User@Host: {user}[{user}] @ {host} [{ip}]  Id:    12")
    stats = "# Query_time: 0.512000  Lock_time: 0.000010 Rows_sent: 20000  Rows_examined: 20000"
    if with_bytes:
        stats += " " + EXTRA.format(recv=recv, sent=sent)
    lines.append(stats)
    if use:
        lines.append(f"use {use};")
    lines.append("SET timestamp=1792152000;")
    lines.extend(query.split("\n"))
    return lines


def parse_all(lines):
    parser = SlowLogParser()
    entries = []
    for line in lines:
        entries.extend(parser.feed(line + "\n"))
    entries.extend(parser.flush())
    return entries


def test_single_entry_with_use():
    entries = parse_all(entry_lines(use="SUPPORT"))
    assert len(entries) == 1
    e = entries[0]
    assert e.user == "report_support"
    assert e.client_ip == "203.0.113.10"
    assert e.schema == "SUPPORT"
    assert e.bytes_sent == 10485760
    assert e.bytes_received == 52
    assert e.rows_sent == 20000
    assert e.rows_examined == 20000
    assert abs(e.query_time - 0.512) < 1e-9
    assert e.query == "SELECT * FROM customers LIMIT 20000;"


def test_second_entry_without_use_inherits_schema():
    lines = entry_lines(use="SUPPORT") + entry_lines(sent=100)
    entries = parse_all(lines)
    assert [e.schema for e in entries] == ["SUPPORT", "SUPPORT"]
    assert entries[1].bytes_sent == 100


def test_schema_switch():
    lines = (
        entry_lines(use="SUPPORT")
        + entry_lines(user="bi_dw", use="DW", sent=200)
        + entry_lines(user="bi_dw", sent=300)
    )
    entries = parse_all(lines)
    assert [e.schema for e in entries] == ["SUPPORT", "DW", "DW"]
    assert [e.bytes_sent for e in entries] == [10485760, 200, 300]


def test_multi_line_query_text():
    query = "SELECT id,\n       name\nFROM customers\nWHERE id > 10;"
    entries = parse_all(entry_lines(use="SUPPORT", query=query))
    assert len(entries) == 1
    assert entries[0].query == query


def test_empty_host_and_ip():
    lines = [
        "# Time: 2026-10-16T12:00:00.123456Z",
        "# User@Host: root[root] @ localhost []  Id:     8",
        "# Query_time: 0.000100  Lock_time: 0.000000 Rows_sent: 1  Rows_examined: 0 "
        + EXTRA.format(recv=20, sent=60),
        "SET timestamp=1792152000;",
        "SELECT 1;",
    ]
    entries = parse_all(lines)
    assert len(entries) == 1
    assert entries[0].user == "root"
    assert entries[0].client_host == "localhost"
    assert entries[0].client_ip == ""
    assert entries[0].schema == "(none)"


def test_missing_bytes_sent_returns_none():
    entries = parse_all(entry_lines(use="DW", with_bytes=False))
    assert len(entries) == 1
    assert entries[0].bytes_sent is None
    assert entries[0].bytes_received is None
    assert entries[0].rows_sent == 20000


def test_startup_header_is_ignored():
    header = [
        "/usr/sbin/mysqld, Version: 8.0.39-0ubuntu0.24.04.2 ((Ubuntu)). started with:",
        "Tcp port: 3306  Unix socket: /var/run/mysqld/mysqld.sock",
        "Time                 Id Command    Argument",
    ]
    entries = parse_all(header + entry_lines(use="FINANCE", user="app_finance"))
    assert len(entries) == 1
    assert entries[0].schema == "FINANCE"
    assert "mysqld" not in entries[0].query
    assert "Tcp port" not in entries[0].query


def test_backticked_db_name():
    entries = parse_all(entry_lines(use="`SALES`", user="etl_sales"))
    assert entries[0].schema == "SALES"


def test_entry_without_time_line():
    lines = entry_lines(use="DW", user="bi_dw") + entry_lines(
        user="app_finance", use="FINANCE", sent=7, with_time=False
    )
    entries = parse_all(lines)
    assert len(entries) == 2
    assert entries[0].user == "bi_dw"
    assert entries[0].schema == "DW"
    assert entries[1].user == "app_finance"
    assert entries[1].schema == "FINANCE"
    assert entries[1].bytes_sent == 7


def test_flush_returns_last_pending_entry():
    parser = SlowLogParser()
    emitted = []
    for line in entry_lines(use="SUPPORT"):
        emitted.extend(parser.feed(line + "\n"))
    assert emitted == []
    pending = parser.flush()
    assert len(pending) == 1
    assert pending[0].schema == "SUPPORT"
    assert parser.flush() == []


def test_feed_emits_previous_entry_when_next_starts():
    parser = SlowLogParser()
    emitted = []
    for line in entry_lines(use="SUPPORT") + ["# Time: 2026-10-16T12:00:01.000000Z"]:
        emitted.extend(parser.feed(line + "\n"))
    assert len(emitted) == 1
    assert emitted[0].schema == "SUPPORT"


def test_schema_state_survives_flush():
    parser = SlowLogParser()
    for line in entry_lines(use="FINANCE"):
        parser.feed(line + "\n")
    parser.flush()
    for line in entry_lines():
        parser.feed(line + "\n")
    assert parser.flush()[0].schema == "FINANCE"
