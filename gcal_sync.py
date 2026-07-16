#!/usr/bin/env python3
"""
gcal-icloud-sync — one-way sync from Google Calendar into an iCloud calendar.

Runs anywhere Python runs; designed for GitHub Actions (see
.github/workflows/sync.yml). All configuration comes from environment
variables — see README.md for setup.

Required:
  ICLOUD_EMAIL           Apple ID email
  ICLOUD_APP_PASSWORD    Apple app-specific password (account.apple.com)
  ICLOUD_CALENDAR_NAME   Exact name of the target iCloud calendar
  GCAL_ICAL_URLS         Google "secret address in iCal format" URL(s),
                         comma- or newline-separated
                         (GCAL_ICAL_URL_1, _2, ... also work)

Optional:
  WINDOW_DAYS            Sync window in days back from today (default 60)
  DELETION_SYNC          "true"/"false" — propagate Google deletions (default true)
  MAX_DELETIONS_PER_RUN  Safety cap on deletions per run (default 25)
  DRY_RUN                "true", or pass --dry-run — report without changing iCloud

State is kept in gcal_sync_state.json next to this script; the workflow
commits it back after each run so state survives between Actions runs.
"""

import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone

# ─── Configuration (from environment) ────────────────────────────────────────


def _require_env(name):
    val = os.environ.get(name, "").strip()
    if not val:
        sys.exit(
            f"ERROR: required environment variable {name} is not set.\n"
            f"See README.md → Setup for the full list of secrets."
        )
    return val


def _collect_ical_urls():
    """
    Gather feed URLs from GCAL_ICAL_URLS (comma/newline separated) and any
    numbered GCAL_ICAL_URL_1, _2, ... variables. At least one is required.
    """
    urls = []
    bulk = os.environ.get("GCAL_ICAL_URLS", "")
    for part in re.split(r"[,\n]", bulk):
        part = part.strip()
        if part:
            urls.append(part)
    i = 1
    while True:
        val = os.environ.get(f"GCAL_ICAL_URL_{i}", "").strip()
        if not val:
            break
        urls.append(val)
        i += 1
    return urls


ICLOUD_EMAIL = _require_env("ICLOUD_EMAIL")
ICLOUD_APP_PASSWORD = _require_env("ICLOUD_APP_PASSWORD")
ICLOUD_CALENDAR_NAME = _require_env("ICLOUD_CALENDAR_NAME")

GOOGLE_ICAL_URLS = _collect_ical_urls()
if not GOOGLE_ICAL_URLS:
    sys.exit(
        "ERROR: no calendar feeds configured. Set GCAL_ICAL_URLS "
        "(comma-separated) or GCAL_ICAL_URL_1, _2, ..."
    )

WINDOW_DAYS = int(os.environ.get("WINDOW_DAYS", "60"))
DELETION_SYNC = os.environ.get("DELETION_SYNC", "true").strip().lower() != "false"
MAX_DELETIONS_PER_RUN = int(os.environ.get("MAX_DELETIONS_PER_RUN", "25"))
DRY_RUN = (
    "--dry-run" in sys.argv
    or os.environ.get("DRY_RUN", "").strip().lower() == "true"
)

# ─────────────────────────────────────────────────────────────────────────────

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gcal_sync_state.json")

# Optional ignore list: events matched here are never created, updated, or
# deleted in iCloud (useful after manually deleting an event's future
# occurrences in Apple Calendar — keeps the sync from resurrecting them).
IGNORE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sync_ignore.txt")

# State file format per UID:
#   {"lm": "last-modified-string", "url": "https://caldav.icloud.com/.../event.ics"}
# The stored URL lets us PUT directly on updates, avoiding duplicate creation.


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def load_ignore_list():
    """One entry per line; matches an event's UID or title, case-insensitive."""
    entries = set()
    if os.path.exists(IGNORE_FILE):
        with open(IGNORE_FILE) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    entries.add(line.lower())
    return entries


def state_get_lm(state, uid):
    """Get last-modified from state, handling both dict and bare-string entries."""
    val = state.get(uid)
    if isinstance(val, dict):
        return val.get("lm", "")
    return val or ""


def state_get_url(state, uid):
    """Get the stored iCloud URL for a UID, if we have it."""
    val = state.get(uid)
    if isinstance(val, dict):
        return val.get("url")
    return None


def state_set(state, uid, last_modified, icloud_url=None):
    """Save UID to state, preserving existing URL if no new one is given."""
    existing_url = state_get_url(state, uid)
    url = icloud_url or existing_url
    if url:
        state[uid] = {"lm": last_modified, "url": url}
    else:
        state[uid] = last_modified


# ─── Date-window filter ──────────────────────────────────────────────────────


def _to_utc(value):
    """Normalize a date or datetime (naive or aware) to an aware UTC datetime."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    return None


def event_in_window(component, cutoff):
    """
    True if the event has (or may have) an occurrence on/after `cutoff`.

    Recurring events (RRULE) are kept unless the recurrence provably ended
    before the cutoff — an open-ended RRULE is always kept, no matter how old
    its DTSTART is. On any parsing doubt we return True: needlessly syncing an
    old event is harmless, while wrongly dropping a live one is not.
    """
    try:
        rrule = component.get("RRULE")
        if rrule:
            until = rrule.get("UNTIL")
            count = rrule.get("COUNT")
            if not until and not count:
                return True  # open-ended recurrence — always in window
            if until:
                u = _to_utc(until[0] if isinstance(until, list) else until)
                return u is None or u >= cutoff
            # COUNT-bounded: expand the rule to find the final occurrence.
            try:
                from dateutil.rrule import rrulestr

                dtstart = _to_utc(component.get("DTSTART").dt)
                rule = rrulestr(
                    rrule.to_ical().decode(), dtstart=dtstart.replace(tzinfo=None)
                )
                occurrences = list(rule)
                if not occurrences:
                    return True
                last = occurrences[-1].replace(tzinfo=timezone.utc)
                return last >= cutoff
            except Exception:
                return True
        # One-off event, or a RECURRENCE-ID override of a single occurrence:
        # judge by when the event ends (falls back to start).
        end_prop = component.get("DTEND") or component.get("DTSTART")
        if end_prop is None:
            return True
        end = _to_utc(end_prop.dt)
        return end is None or end >= cutoff
    except Exception:
        return True


# ─── iCloud CalDAV helpers ───────────────────────────────────────────────────


def _auth_header():
    credentials = base64.b64encode(
        f"{ICLOUD_EMAIL}:{ICLOUD_APP_PASSWORD}".encode()
    ).decode()
    return f"Basic {credentials}"


def icloud_put(url, event_ical, etag=None):
    """
    PUT an event to a specific iCloud URL (create or update).
    Pass etag to send If-Match (required by iCloud for some updates).
    """
    if DRY_RUN:
        print(f"    [dry-run] would PUT {url.rsplit('/', 1)[-1]}")
        return url
    headers = {
        "Content-Type": "text/calendar; charset=utf-8",
        "Authorization": _auth_header(),
    }
    if etag:
        headers["If-Match"] = etag
    req = urllib.request.Request(
        url, data=event_ical.encode("utf-8"), method="PUT", headers=headers
    )
    with urllib.request.urlopen(req):
        return url


def icloud_delete(url):
    """DELETE an event resource in iCloud (404 = already gone)."""
    if DRY_RUN:
        print(f"    [dry-run] would DELETE {url.rsplit('/', 1)[-1]}")
        return
    req = urllib.request.Request(
        url, method="DELETE", headers={"Authorization": _auth_header()}
    )
    with urllib.request.urlopen(req, timeout=30):
        return


def icloud_get_etag(url):
    """GET an event URL and return its current ETag header (or None)."""
    req = urllib.request.Request(url, headers={"Authorization": _auth_header()})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.headers.get("ETag")


def guess_event_url(cal_url, uid):
    """
    iCloud usually stores events at <calendar>/<UID>.ics. When the CalDAV
    lookup query fails (iCloud sometimes answers REPORT with 412), probe that
    URL directly. Returns (url, etag) if it exists, (None, None) on 404.
    """
    from urllib.parse import quote

    candidate = cal_url.rstrip("/") + "/" + quote(str(uid), safe="") + ".ics"
    try:
        etag = icloud_get_etag(candidate)
        return candidate, etag
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, None
        raise


def find_event_anywhere(calendars, uid, first=None):
    """
    Probe <calendar>/<UID>.ics across every calendar in the account (target
    calendar first). Finds events that were moved to another calendar by the
    user, e.g. for color-coding. Returns (url, etag) or (None, None).
    """
    ordered = ([first] if first is not None else []) + [
        c for c in calendars if c is not first
    ]
    for cal in ordered:
        try:
            url, etag = guess_event_url(str(cal.url), uid)
        except Exception:
            continue
        if url:
            return url, etag
    return None, None


def put_with_etag_retry(url, event_ical):
    """
    PUT to a known event URL; if iCloud answers 412 Precondition Failed,
    fetch the event's current ETag and retry once with If-Match.
    """
    try:
        return icloud_put(url, event_ical)
    except urllib.error.HTTPError as e:
        if e.code != 412:
            raise
    etag = icloud_get_etag(url)
    if not etag:
        raise urllib.error.HTTPError(url, 412, "Precondition Failed (no ETag)", None, None)
    return icloud_put(url, event_ical, etag=etag)


def get_calendars(client, name):
    """Return (target_calendar, all_calendars) for the account."""
    principal = client.principal()
    all_calendars = principal.calendars()
    available = []
    target = None
    for cal in all_calendars:
        try:
            cal_name = cal.get_display_name()
        except Exception:
            cal_name = getattr(cal, "name", "?")
        available.append(cal_name)
        if cal_name == name:
            target = cal
    if target is None:
        raise ValueError(
            f"Calendar '{name}' not found in iCloud.\n"
            f"Available calendars: {available}\n"
            f"Set the ICLOUD_CALENDAR_NAME secret to match one exactly."
        )
    return target, all_calendars


# ─── iCal processing ─────────────────────────────────────────────────────────


def strip_attendees(ical_text):
    """
    Remove ATTENDEE, ORGANIZER, and ROLE=REQ-PARTICIPANT lines from raw iCal
    text, including their folded continuation lines (lines starting with a space).
    Prevents iCloud from re-sending invitations to every attendee when the
    event is uploaded — without this, syncing an event with guests spams them.
    """
    clean_lines = []
    skip = False
    for line in ical_text.splitlines(keepends=True):
        if line.startswith("BEGIN:VEVENT"):
            skip = False
        if line.startswith(("ATTENDEE;", "ORGANIZER;", "ATTENDEE:", "ORGANIZER:")):
            skip = True
            continue
        if skip and (line.startswith(" ") or line.startswith("\t")):
            continue
        if skip and not (line.startswith(" ") or line.startswith("\t")):
            skip = False
        if "ROLE=REQ-PARTICIPANT" in line:
            continue
        clean_lines.append(line)
    return "".join(clean_lines)


# ─── Main sync ───────────────────────────────────────────────────────────────


def sync():
    import caldav
    from icalendar import Calendar as iCalendar

    try:
        from caldav.error import NotFoundError as _NotFoundError
    except ImportError:
        try:
            _NotFoundError = caldav.error.NotFoundError
        except AttributeError:
            _NotFoundError = None

    state = load_state()
    ignore = load_ignore_list()
    cutoff = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)

    client = caldav.DAVClient(
        url="https://caldav.icloud.com",
        username=ICLOUD_EMAIL,
        password=ICLOUD_APP_PASSWORD,
    )
    target_cal, all_calendars = get_calendars(client, ICLOUD_CALENDAR_NAME)
    mode = " [DRY RUN — no changes will be made]" if DRY_RUN else ""
    print(f"Connected to iCloud → syncing into '{ICLOUD_CALENDAR_NAME}' "
          f"({len(all_calendars)} calendars visible){mode}\n")

    new_count = updated_count = skipped_count = exists_count = windowed_out = 0
    deleted_count = ignored_count = 0
    seen_uids = set()   # every UID present in any feed (window-filtered or not)
    feeds_failed = False

    for idx, url in enumerate(GOOGLE_ICAL_URLS, 1):
        print(f"Fetching calendar feed #{idx}...")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "gcal-icloud-sync/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
        except Exception as e:
            print(f"  Error fetching feed #{idx}: {type(e).__name__}: {e}")
            feeds_failed = True
            continue

        cal = iCalendar.from_ical(data)

        # Group VEVENTs by UID. A recurring event with modified occurrences
        # ships as several VEVENTs sharing one UID (the master plus
        # RECURRENCE-ID overrides). All components of a UID must be uploaded
        # together in a single VCALENDAR — uploading them one at a time makes
        # iCloud reject the siblings with 412 Precondition Failed.
        groups = {}
        for component in cal.walk():
            if component.name != "VEVENT":
                continue
            uid = str(component.get("UID", ""))
            if not uid:
                continue
            groups.setdefault(uid, []).append(component)

        seen_uids.update(groups)

        for uid, comps in groups.items():
            title = str(comps[0].get("SUMMARY", "Untitled"))

            if uid.lower() in ignore or title.lower() in ignore:
                ignored_count += 1
                continue

            if not any(event_in_window(c, cutoff) for c in comps):
                windowed_out += 1
                continue

            # Newest LAST-MODIFIED across the master and all overrides.
            last_modified = max(
                str(c.get("LAST-MODIFIED", c.get("DTSTAMP", ""))) for c in comps
            )

            if state_get_lm(state, uid) == last_modified:
                skipped_count += 1
                continue

            body = "".join(
                strip_attendees(c.to_ical().decode("utf-8", errors="replace"))
                for c in comps
            )
            event_ical = (
                "BEGIN:VCALENDAR\r\n"
                "VERSION:2.0\r\n"
                "PRODID:-//gcal-icloud-sync//EN\r\n"
                "METHOD:PUBLISH\r\n"
                + body
                + "END:VCALENDAR\r\n"
            )

            # If we have a stored iCloud URL for this event, PUT directly to it —
            # this is a guaranteed update with no duplicate risk.
            stored_url = state_get_url(state, uid)
            if stored_url:
                try:
                    put_with_etag_retry(stored_url, event_ical)
                    updated_count += 1
                    print(f"  ✓ Updated:  {title}")
                    state_set(state, uid, last_modified, stored_url)
                    continue
                except Exception as e:
                    print(f"  ⚠ Stored URL update failed for '{title}': {e} — falling back")

            # No stored URL — look the event up in iCloud by UID.
            existing = None
            lookup_err = None
            try:
                existing = target_cal.event_by_uid(uid)
            except Exception as e:
                lookup_err = e

            if existing is not None:
                # Found — update at its real URL, with ETag retry on 412.
                try:
                    saved_url = str(existing.url)
                    put_with_etag_retry(saved_url, event_ical)
                    updated_count += 1
                    print(f"  ✓ Updated:  {title}")
                    state_set(state, uid, last_modified, saved_url)
                except Exception as e:
                    print(f"  ✗ Update failed for '{title}': {e}")
                continue

            error_str = str(lookup_err)
            is_not_found = (
                (_NotFoundError and isinstance(lookup_err, _NotFoundError))
                or "404" in error_str
                or "not found" in error_str.lower()
                or "notfound" in type(lookup_err).__name__.lower()
            )
            if not is_not_found:
                # Lookup query failed (iCloud sometimes 412s REPORT queries).
                # Probe <calendar>/<UID>.ics across every calendar directly.
                try:
                    guessed_url, etag = find_event_anywhere(all_calendars, uid, first=target_cal)
                except Exception as e:
                    print(f"  ✗ Lookup error on '{title}': {lookup_err}; URL probe failed: {e}")
                    continue
                if guessed_url:
                    try:
                        icloud_put(guessed_url, event_ical, etag=etag)
                        updated_count += 1
                        print(f"  ✓ Updated (via probed URL):  {title}")
                        state_set(state, uid, last_modified, guessed_url)
                    except Exception as e:
                        print(f"  ✗ Update at probed URL failed for '{title}': {e}")
                    continue
                # Probe says 404 everywhere — fall through and create it.

            # Not found in iCloud — create it.
            if DRY_RUN:
                new_count += 1
                print(f"  [dry-run] Would create:  {title}")
                continue
            try:
                target_cal.add_event(event_ical)
                # Try to retrieve the URL caldav used
                try:
                    created = target_cal.event_by_uid(uid)
                    saved_url = str(created.url)
                except Exception:
                    saved_url = None
                new_count += 1
                print(f"  ✓ Created:  {title}")
                state_set(state, uid, last_modified, saved_url)

            except Exception as create_err:
                create_str = str(create_err)
                if "412" in create_str or "precondition" in create_str.lower():
                    # UID conflict — the event exists somewhere in the account.
                    # Probe every calendar: the user may have moved it out of
                    # the target calendar.
                    try:
                        found_url, found_etag = find_event_anywhere(
                            all_calendars, uid, first=target_cal
                        )
                    except Exception:
                        found_url, found_etag = None, None
                    if found_url:
                        try:
                            icloud_put(found_url, event_ical, etag=found_etag)
                            updated_count += 1
                            print(f"  ✓ Updated (moved copy):  {title}")
                            state_set(state, uid, last_modified, found_url)
                            continue
                        except Exception as e:
                            print(f"  ⚠ Moved-copy update failed for '{title}': {e}")
                    # Not found anywhere — force create at a fresh random URL
                    try:
                        random_url = str(target_cal.url).rstrip("/") + "/" + str(uuid.uuid4()) + ".ics"
                        icloud_put(random_url, event_ical)
                        new_count += 1
                        print(f"  ✓ Created:  {title}")
                        state_set(state, uid, last_modified, random_url)
                    except Exception as force_err:
                        force_str = str(force_err)
                        if "412" in force_str or "409" in force_str:
                            # UID is occupied in iCloud but unreachable (e.g. a
                            # tombstoned/truncated series after a manual delete).
                            # Nothing can be done server-side; mark handled so we
                            # only retry if the Google event changes again.
                            exists_count += 1
                            print(f"  ⚠ UID occupied in iCloud, leaving as-is: {title}")
                            state_set(state, uid, last_modified)
                        else:
                            print(f"  ✗ Create failed for '{title}': {force_err}")
                else:
                    print(f"  ✗ Create failed for '{title}': {create_err}")

    # ── Deletion sync: previously synced UIDs that vanished from all feeds ──
    if not DELETION_SYNC:
        pass
    elif feeds_failed:
        print("\nSkipping deletion check — a feed failed to fetch this run.")
    else:
        missing = [uid for uid in list(state.keys()) if uid not in seen_uids]
        if len(missing) > MAX_DELETIONS_PER_RUN:
            print(f"\n⚠ {len(missing)} previously synced events vanished from the feeds "
                  f"(cap {MAX_DELETIONS_PER_RUN}) — likely a feed glitch; deleting nothing.")
        elif missing:
            print(f"\n{len(missing)} event(s) deleted in Google — removing from iCloud...")
            for uid in missing:
                if uid.lower() in ignore:
                    continue
                url = state_get_url(state, uid)
                if not url:
                    try:
                        url, _ = find_event_anywhere(all_calendars, uid, first=target_cal)
                    except Exception as e:
                        print(f"  ✗ Delete probe failed for {uid}: {e} — will retry")
                        continue
                if url:
                    try:
                        icloud_delete(url)
                    except urllib.error.HTTPError as e:
                        if e.code != 404:  # 404 = already gone, fine
                            print(f"  ✗ Delete failed ({e.code}) for {uid} — will retry")
                            continue
                    except Exception as e:
                        print(f"  ✗ Delete failed for {uid}: {e} — will retry")
                        continue
                # Deleted (or never findable in iCloud) — forget it.
                if not DRY_RUN:
                    del state[uid]
                deleted_count += 1
                print(f"  ✓ Deleted from iCloud: {uid}")

    if DRY_RUN:
        print("\n[dry-run] State file NOT saved; iCloud untouched.")
    else:
        save_state(state)
    print(f"\nDone — {new_count} created, {updated_count} updated, {deleted_count} deleted, "
          f"{exists_count} already existed, {skipped_count} unchanged, {ignored_count} ignored, "
          f"{windowed_out} outside {WINDOW_DAYS}-day window.")


if __name__ == "__main__":
    sync()
