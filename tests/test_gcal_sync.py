"""Unit tests for the pure logic in gcal_sync.py (no network required)."""

import os
import sys

# The module reads config at import time — stub it before importing.
os.environ.setdefault("ICLOUD_EMAIL", "test@example.com")
os.environ.setdefault("ICLOUD_APP_PASSWORD", "test-test-test-test")
os.environ.setdefault("ICLOUD_CALENDAR_NAME", "Test")
os.environ.setdefault("GCAL_ICAL_URLS", "https://example.com/basic.ics")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime, timedelta, timezone  # noqa: E402

from icalendar import Calendar  # noqa: E402

import gcal_sync as g  # noqa: E402

CUTOFF = datetime.now(timezone.utc) - timedelta(days=60)


def ev(body):
    """Build a single VEVENT component from an iCal body fragment."""
    cal = Calendar.from_ical(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:t\r\n"
        + body
        + "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    return [c for c in cal.walk() if c.name == "VEVENT"][0]


def future(days):
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y%m%dT%H%M%SZ")


# ── Window filter ────────────────────────────────────────────────────────────

def test_old_oneoff_filtered():
    assert g.event_in_window(ev("DTSTART:20230105T100000Z\r\nDTEND:20230105T110000Z\r\n"), CUTOFF) is False


def test_recent_oneoff_kept():
    assert g.event_in_window(ev(f"DTSTART:{future(-30)}\r\n"), CUTOFF) is True


def test_future_oneoff_kept():
    assert g.event_in_window(ev(f"DTSTART:{future(300)}\r\n"), CUTOFF) is True


def test_old_allday_filtered():
    assert g.event_in_window(ev("DTSTART;VALUE=DATE:20220301\r\nDTEND;VALUE=DATE:20220302\r\n"), CUTOFF) is False


def test_open_ended_rrule_always_kept():
    # DTSTART years ago but no UNTIL/COUNT — must be kept.
    assert g.event_in_window(ev("DTSTART:20190101T100000Z\r\nRRULE:FREQ=WEEKLY;BYDAY=WE\r\n"), CUTOFF) is True


def test_rrule_until_past_filtered():
    assert g.event_in_window(ev("DTSTART:20200101T100000Z\r\nRRULE:FREQ=WEEKLY;UNTIL=20210101T000000Z\r\n"), CUTOFF) is False


def test_rrule_until_future_kept():
    assert g.event_in_window(ev(f"DTSTART:20200101T100000Z\r\nRRULE:FREQ=WEEKLY;UNTIL={future(300)}\r\n"), CUTOFF) is True


def test_rrule_count_ended_filtered():
    assert g.event_in_window(ev("DTSTART:20200101T100000Z\r\nRRULE:FREQ=DAILY;COUNT=5\r\n"), CUTOFF) is False


def test_rrule_count_active_kept():
    # Monthly x5 starting 30 days ago ends well inside the window.
    start = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y%m%dT%H%M%SZ")
    assert g.event_in_window(ev(f"DTSTART:{start}\r\nRRULE:FREQ=MONTHLY;COUNT=5\r\n"), CUTOFF) is True


def test_recurrence_override_recent_kept():
    t = future(-10)
    assert g.event_in_window(ev(f"DTSTART:{t}\r\nRECURRENCE-ID:{t}\r\n"), CUTOFF) is True


def test_recurrence_override_old_filtered():
    assert g.event_in_window(ev("DTSTART:20230501T100000Z\r\nRECURRENCE-ID:20230501T100000Z\r\n"), CUTOFF) is False


def test_dateless_event_kept():
    # No parseable dates — keep rather than risk dropping a live event.
    assert g.event_in_window(ev("SUMMARY:weird\r\n"), CUTOFF) is True


def test_tzid_datetime_old_filtered():
    assert g.event_in_window(ev("DTSTART;TZID=America/New_York:20230105T100000\r\n"), CUTOFF) is False


# ── UID grouping / last-modified ─────────────────────────────────────────────

GROUP_ICS = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
    "BEGIN:VEVENT\r\nUID:ga\r\nSUMMARY:Shift\r\nDTSTART:20260601T090000Z\r\n"
    "LAST-MODIFIED:20260701T000000Z\r\nRRULE:FREQ=WEEKLY\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nUID:ga\r\nSUMMARY:Shift\r\nDTSTART:20260708T100000Z\r\n"
    "RECURRENCE-ID:20260708T090000Z\r\nLAST-MODIFIED:20260709T000000Z\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nUID:other\r\nSUMMARY:Solo\r\nDTSTART:20260710T090000Z\r\n"
    "LAST-MODIFIED:20260601T000000Z\r\nEND:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


def _group(ics):
    groups = {}
    for c in Calendar.from_ical(ics).walk():
        if c.name != "VEVENT":
            continue
        groups.setdefault(str(c.get("UID")), []).append(c)
    return groups


def test_same_uid_components_grouped():
    groups = _group(GROUP_ICS)
    assert len(groups) == 2
    assert len(groups["ga"]) == 2


def test_group_lm_is_newest():
    groups = _group(GROUP_ICS)
    lms = [str(c.get("LAST-MODIFIED", c.get("DTSTAMP", ""))) for c in groups["ga"]]
    assert max(lms) == lms[1]  # the override was modified later and must win


def test_group_payload_contains_all_components():
    groups = _group(GROUP_ICS)
    body = "".join(g.strip_attendees(c.to_ical().decode()) for c in groups["ga"])
    assert body.count("BEGIN:VEVENT") == 2
    assert "RECURRENCE-ID" in body


# ── Attendee stripping ───────────────────────────────────────────────────────

def test_attendees_and_organizer_stripped():
    raw = (
        "BEGIN:VEVENT\r\nUID:x\r\nSUMMARY:Meet\r\n"
        "ORGANIZER;CN=Someone:mailto:someone@example.com\r\n"
        "ATTENDEE;CUTYPE=INDIVIDUAL;ROLE=REQ-PARTICIPANT;CN=Guest\r\n"
        " :mailto:guest@example.com\r\n"
        "DTSTART:20260101T100000Z\r\nEND:VEVENT\r\n"
    )
    out = g.strip_attendees(raw)
    assert "ATTENDEE" not in out
    assert "ORGANIZER" not in out
    assert "guest@example.com" not in out          # folded continuation removed too
    assert "SUMMARY:Meet" in out                   # everything else intact


# ── URL collection / ignore list / state ────────────────────────────────────

def test_collect_urls_comma_and_numbered(monkeypatch):
    monkeypatch.setenv("GCAL_ICAL_URLS", "https://a/basic.ics, https://b/basic.ics")
    monkeypatch.setenv("GCAL_ICAL_URL_1", "https://c/basic.ics")
    assert g._collect_ical_urls() == [
        "https://a/basic.ics", "https://b/basic.ics", "https://c/basic.ics",
    ]


def test_collect_urls_empty(monkeypatch):
    monkeypatch.setenv("GCAL_ICAL_URLS", "")
    monkeypatch.delenv("GCAL_ICAL_URL_1", raising=False)
    assert g._collect_ical_urls() == []


def test_ignore_list_matching(tmp_path, monkeypatch):
    f = tmp_path / "sync_ignore.txt"
    f.write_text("# comment\nOld Shift\nsome-uid@google.com\n")
    monkeypatch.setattr(g, "IGNORE_FILE", str(f))
    ig = g.load_ignore_list()
    assert "old shift" in ig
    assert "some-uid@google.com" in ig
    assert "# comment" not in ig


def test_state_roundtrip_preserves_url():
    state = {}
    g.state_set(state, "u1", "lm1", "https://caldav/x.ics")
    g.state_set(state, "u1", "lm2")  # no URL passed — must keep the old one
    assert g.state_get_lm(state, "u1") == "lm2"
    assert g.state_get_url(state, "u1") == "https://caldav/x.ics"


def test_state_backcompat_bare_string():
    state = {"u2": "some-lm"}
    assert g.state_get_lm(state, "u2") == "some-lm"
    assert g.state_get_url(state, "u2") is None


def test_guess_event_url_encoding(monkeypatch):
    seen = {}
    monkeypatch.setattr(g, "icloud_get_etag", lambda u: seen.setdefault("url", u) and "etag" or "etag")
    url, etag = g.guess_event_url("https://caldav.icloud.com/123/cal/", "abc 123@google.com")
    assert seen["url"] == "https://caldav.icloud.com/123/cal/abc%20123%40google.com.ics"
    assert etag == "etag"
