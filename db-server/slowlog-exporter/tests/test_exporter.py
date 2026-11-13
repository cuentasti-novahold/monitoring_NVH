"""Tests for the exporter: command line options and file tailing.

The tailing tests write a slow log file the way MySQL does (appending while
the exporter keeps the file open), with both LF and CRLF line endings, so the
exporter also works when the log comes from a Windows host.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from prometheus_client import REGISTRY  # noqa: E402

from slowlog_exporter import Exporter, parse_args  # noqa: E402


def entry(user, ip, sent, use=None):
    lines = [
        "# Time: 2026-10-17T10:00:00.000000Z",
        f"# User@Host: {user}[{user}] @ localhost [{ip}]  Id:     8",
        f"# Query_time: 0.010000  Lock_time: 0.000001 Rows_sent: 10  Rows_examined: 10 "
        f"Thread_id: 8 Errno: 0 Killed: 0 Bytes_received: 40 Bytes_sent: {sent} Read_first: 0",
    ]
    if use:
        lines.append(f"use {use};")
    lines += ["SET timestamp=1792231200;", "SELECT 1;"]
    return lines


def sample(schema, user, ip):
    return REGISTRY.get_sample_value(
        "mysql_slowlog_bytes_sent_total", {"schema": schema, "user": user, "client_ip": ip}
    ) or 0.0


# -- command line ---------------------------------------------------------------


def test_listen_address_defaults_to_all_interfaces():
    args = parse_args([])
    assert args.listen_address == "0.0.0.0"
    assert args.port == 9105


def test_listen_address_can_be_loopback():
    args = parse_args(["--listen-address", "127.0.0.1", "--log-file", r"C:\data\slow.log"])
    assert args.listen_address == "127.0.0.1"
    assert args.log_file == r"C:\data\slow.log"


def test_main_serves_metrics_on_the_requested_address(monkeypatch):
    import slowlog_exporter

    calls = []
    monkeypatch.setattr(slowlog_exporter, "start_http_server", lambda port, addr: calls.append((port, addr)))
    monkeypatch.setattr(slowlog_exporter.Exporter, "run", lambda self, from_beginning: None)
    monkeypatch.setattr(slowlog_exporter.signal, "signal", lambda *args: None)
    monkeypatch.setattr(sys, "argv", ["slowlog_exporter.py", "--listen-address", "127.0.0.1", "--port", "9200"])

    slowlog_exporter.main()

    assert calls == [(9200, "127.0.0.1")]


# -- tailing ----------------------------------------------------------------------


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_tails_lines_appended_after_open(tmp_path, newline):
    ip = "127.0.0.42" if newline == "\n" else "127.0.0.43"
    path = tmp_path / "slow.log"
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(newline.join(entry("app_finance", ip, 100, use="FINANCE")) + newline)

    exp = Exporter(str(path), exclude_users=set())
    assert exp.open(from_beginning=False)  # learns schema FINANCE, emits nothing
    before = sample("FINANCE", "app_finance", ip)

    with open(path, "a", encoding="utf-8", newline="") as f:  # MySQL keeps appending
        f.write(newline.join(entry("app_finance", ip, 250)) + newline)
        f.write(newline.join(entry("app_finance", ip, 750)) + newline)
    assert exp.read_available()
    exp.flush()
    exp.handle.close()

    assert sample("FINANCE", "app_finance", ip) - before == 1000


def test_partial_line_waits_for_newline(tmp_path):
    ip = "127.0.0.44"
    path = tmp_path / "slow.log"
    path.write_bytes(b"")
    exp = Exporter(str(path), exclude_users=set())
    assert exp.open(from_beginning=True)

    lines = entry("bi_dw", ip, 300, use="DW")
    text = "\r\n".join(lines) + "\r\n"
    cut = text.index("Bytes_sent: 300") + len("Bytes_sent: 3")  # split inside a number
    with open(path, "ab") as f:
        f.write(text[:cut].encode())
    exp.read_available()
    with open(path, "ab") as f:
        f.write(text[cut:].encode())
    exp.read_available()
    exp.flush()
    exp.handle.close()

    assert sample("DW", "bi_dw", ip) == 300


def test_truncation_is_detected(tmp_path):
    path = tmp_path / "slow.log"
    path.write_text("\n".join(entry("bi_dw", "127.0.0.45", 1)) + "\n", encoding="utf-8")
    exp = Exporter(str(path), exclude_users=set())
    assert exp.open(from_beginning=False)
    with open(path, "w", encoding="utf-8"):
        pass  # truncate in place
    assert exp.rotated_or_truncated() == "truncated"
    exp.handle.close()



# -- client_ip label ----------------------------------------------------------------


def test_entry_without_ip_is_labelled_as_a_host_not_as_an_ip(tmp_path):
    # Local socket connections have no IP: the label must not look like one.
    path = tmp_path / "slow.log"
    lines = entry("bi_dw", "", 77, use="DW")
    lines[1] = "# User@Host: bi_dw[bi_dw] @ localhost []  Id:     8"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    exp = Exporter(str(path), exclude_users=set())
    assert exp.open(from_beginning=True)
    exp.read_available()
    exp.flush()
    exp.handle.close()
    assert sample("DW", "bi_dw", "host:localhost") == 77
    assert sample("DW", "bi_dw", "localhost") == 0


# -- startup, restart and rotation (F3) -------------------------------------------


def write(path, *entries_lines, mode="a"):
    with open(path, mode, encoding="utf-8", newline="") as f:
        for lines in entries_lines:
            f.write("\n".join(lines) + "\n")


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_startup_scan_stops_at_the_size_seen_when_opening(tmp_path, monkeypatch):
    # Entries MySQL appends while the history is being scanned must be counted.
    ip = "127.0.1.1"
    path = tmp_path / "slow.log"
    write(path, entry("bi_dw", ip, 100, use="DW"), mode="w")
    history_size = path.stat().st_size
    write(path, entry("bi_dw", ip, 250))  # arrives "during" the scan
    monkeypatch.setattr(Exporter, "_snapshot_size", lambda self: history_size)

    exp = Exporter(str(path), exclude_users=set())
    assert exp.open(from_beginning=False)
    exp.read_available()
    exp.flush()
    exp.handle.close()
    assert sample("DW", "bi_dw", ip) == 250


def test_restart_resumes_from_the_saved_position(tmp_path):
    ip = "127.0.1.2"
    path, state = tmp_path / "slow.log", tmp_path / "state.json"
    write(path, entry("bi_dw", ip, 1, use="DW"), mode="w")  # history, never counted

    first = Exporter(str(path), exclude_users=set(), state_file=str(state))
    assert first.open(from_beginning=False)
    write(path, entry("bi_dw", ip, 100))
    first.read_available()  # the 100-byte entry is still pending (not emitted)
    first.save_state()
    first.handle.close()  # crash: the pending entry is lost in this process
    assert sample("DW", "bi_dw", ip) == 0

    write(path, entry("bi_dw", ip, 200))  # written while the exporter was down
    second = Exporter(str(path), exclude_users=set(), state_file=str(state))
    assert second.open(from_beginning=False)
    second.read_available()
    second.flush()
    second.handle.close()
    # Both entries counted exactly once, history still not counted, schema kept.
    assert sample("DW", "bi_dw", ip) == 300


def test_restart_after_rotation_reads_the_new_file_from_the_start(tmp_path):
    ip = "127.0.1.3"
    path, state = tmp_path / "slow.log", tmp_path / "state.json"
    write(path, entry("app_finance", ip, 1, use="FINANCE"), mode="w")
    first = Exporter(str(path), exclude_users=set(), state_file=str(state))
    assert first.open(from_beginning=False)
    first.save_state()
    first.handle.close()

    os.replace(path, tmp_path / "slow.log.1")  # rotated while down
    write(path, entry("app_finance", ip, 400, use="FINANCE"), mode="w")
    second = Exporter(str(path), exclude_users=set(), state_file=str(state))
    assert second.open(from_beginning=False)
    second.read_available()
    second.flush()
    second.handle.close()
    assert sample("FINANCE", "app_finance", ip) == 400


@pytest.mark.skipif(sys.platform == "win32", reason="renaming an open file needs POSIX semantics (Linux)")
def test_rotation_keeps_reading_the_old_file_until_it_goes_quiet(tmp_path):
    # logrotate renames and creates the new file before `flush-logs slow`,
    # so MySQL keeps writing to the old inode for a moment.
    ip = "127.0.1.4"
    path, old = tmp_path / "slow.log", tmp_path / "slow.log.1"
    write(path, entry("report_support", ip, 1, use="SUPPORT"), mode="w")
    clock = Clock()
    exp = Exporter(str(path), exclude_users=set(), clock=clock)
    assert exp.open(from_beginning=False)

    os.replace(path, old)
    write(path, [], mode="w")  # new, empty file already in place
    exp.step()  # rotation noticed
    write(old, entry("report_support", ip, 500))  # MySQL still on the old inode
    clock.now += 2
    exp.step()
    clock.now += 30  # old file quiet for longer than the grace period
    exp.step()
    write(path, entry("report_support", ip, 70, use="SUPPORT"))  # after flush-logs
    clock.now += 1
    exp.step()
    exp.flush()
    exp.handle.close()
    assert sample("SUPPORT", "report_support", ip) == 570


@pytest.mark.skipif(sys.platform == "win32", reason="renaming an open file needs POSIX semantics (Linux)")
def test_missing_file_is_logged_and_the_old_handle_keeps_being_read(tmp_path, caplog):
    ip = "127.0.1.5"
    path = tmp_path / "slow.log"
    write(path, entry("bi_dw", ip, 1, use="DW"), mode="w")
    clock = Clock()
    exp = Exporter(str(path), exclude_users=set(), clock=clock)
    assert exp.open(from_beginning=False)

    moved = tmp_path / "moved.log"
    os.replace(path, moved)
    with caplog.at_level("WARNING"):
        exp.step()
    assert "missing" in caplog.text
    write(moved, entry("bi_dw", ip, 60))
    clock.now += 1
    exp.step()
    exp.flush()
    exp.handle.close()
    assert sample("DW", "bi_dw", ip) == 60


def test_state_write_failure_is_logged_and_does_not_stop_the_exporter(tmp_path, caplog):
    # e.g. the state volume is full or read-only: counting must go on.
    ip = "127.0.1.6"
    path = tmp_path / "slow.log"
    write(path, entry("bi_dw", ip, 1, use="DW"), mode="w")
    unwritable = tmp_path / "no-such-dir" / "state.json"
    clock = Clock()
    exp = Exporter(str(path), exclude_users=set(), state_file=str(unwritable), clock=clock)
    assert exp.open(from_beginning=False)
    write(path, entry("bi_dw", ip, 90))
    with caplog.at_level("WARNING"):
        clock.now += 10
        exp.step()  # reads the entry and tries to save the state
        clock.now += 10
        exp.step()
    exp.flush()
    exp.handle.close()
    assert "could not save state" in caplog.text
    assert caplog.text.count("could not save state") == 1  # not repeated every poll
    assert sample("DW", "bi_dw", ip) == 90


def test_state_saving_recovers_after_a_failure_and_warns_again_on_a_new_one(tmp_path, caplog):
    ip = "127.0.1.7"
    path = tmp_path / "slow.log"
    write(path, entry("bi_dw", ip, 1, use="DW"), mode="w")
    state_dir = tmp_path / "state"
    state = state_dir / "state.json"
    clock = Clock()
    exp = Exporter(str(path), exclude_users=set(), state_file=str(state), clock=clock)
    assert exp.open(from_beginning=False)

    with caplog.at_level("INFO"):
        clock.now += 10
        exp.step()  # directory missing: first failure, warned
        state_dir.mkdir()
        clock.now += 10
        exp.step()  # works again
        assert state.exists()
        assert "state saved again" in caplog.text

        state.unlink()
        state_dir.rmdir()
        clock.now += 10
        exp.step()  # a new failure is warned about again
    exp.handle.close()
    assert caplog.text.count("could not save state") == 2


def test_state_save_failures_are_counted_for_alerting(tmp_path):
    ip = "127.0.1.8"
    path = tmp_path / "slow.log"
    write(path, entry("bi_dw", ip, 1, use="DW"), mode="w")
    before = REGISTRY.get_sample_value("mysql_slowlog_state_save_errors_total") or 0.0
    exp = Exporter(str(path), exclude_users=set(), state_file=str(tmp_path / "missing" / "s.json"))
    assert exp.open(from_beginning=False)
    exp.save_state()
    exp.save_state()
    exp.handle.close()
    assert REGISTRY.get_sample_value("mysql_slowlog_state_save_errors_total") - before == 2
