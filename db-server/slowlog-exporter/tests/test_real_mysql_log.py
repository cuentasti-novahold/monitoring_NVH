"""Regression test against a REAL MySQL 8.0.40 slow log excerpt.

The fixture was captured from the local Docker mode (local/docker-compose.yml)
with log_slow_extra=ON and long_query_time=0, while the simulated consumers were
running. Client IPs come from the Docker networks that mirror production:
198.51.100.10 = "internet" consumer, 10.20.32.20 = internal consumer.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from slowlog_parser import SlowLogParser  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "mysql-8.0.40-slow.log")


def parse_fixture():
    parser = SlowLogParser()
    entries = []
    with open(FIXTURE, encoding="utf-8") as handle:
        for line in handle:
            entries.extend(parser.feed(line))
    entries.extend(parser.flush())
    return entries


def test_every_real_entry_carries_bytes_sent():
    entries = parse_fixture()
    assert len(entries) == 29
    assert all(e.bytes_sent is not None for e in entries)


def test_heavy_internet_query_is_attributed_to_schema_user_and_client_ip():
    heavy = [e for e in parse_fixture() if e.query.startswith("SELECT * FROM customers LIMIT 20000")]
    assert heavy, "expected the heavy SUPPORT report query in the fixture"
    first = heavy[0]
    assert first.schema == "SUPPORT"
    assert first.user == "report_support"
    assert first.client_ip == "198.51.100.10"
    assert first.rows_sent == 20000
    assert first.bytes_sent == 9858235


def test_totals_per_schema_user_and_client_ip():
    totals = {}
    for e in parse_fixture():
        key = (e.schema, e.user, e.client_ip)
        totals[key] = totals.get(key, 0) + e.bytes_sent
    assert totals[("SUPPORT", "report_support", "198.51.100.10")] == 19716481
    assert totals[("FINANCE", "app_finance", "10.20.32.20")] == 2109176
    assert totals[("DW", "bi_dw", "198.51.100.10")] == 632


def test_cross_schema_query_is_attributed_to_the_default_schema():
    # analyst_multi connects with default schema DW but reads FINANCE.payments.
    # Its entries carry no `use` line of their own: MySQL writes `use` only when
    # the schema differs from the last one written to the log, and the previous
    # entry (bi_dw) had already switched it to DW. Either way the attribution is
    # the connection's default schema, never the schema of the tables read
    # (documented limitation).
    cross = [e for e in parse_fixture() if e.user == "analyst_multi" and "FINANCE.payments" in e.query]
    assert cross
    assert {e.schema for e in cross} == {"DW"}
