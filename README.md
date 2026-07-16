# gcal-icloud-sync

One-way sync from Google Calendar into an iCloud calendar of your choice, running free on GitHub Actions — no server, no OAuth app registration, no subscription.

Paid services (CalendarBridge, SyncGene, OneCal) will host this sync for $5–15/month. Free open-source tools exist but can't write into iCloud. This tool fills the gap: your Google events land in a real iCloud calendar (so they behave like native Apple Calendar events — color-coding, widgets, Siri, sharing), your credentials never leave your own GitHub repository, and the whole thing runs on GitHub's free Actions tier.

## How it works

- Every hour, a GitHub Actions job fetches your Google calendars' secret iCal feeds and uploads new or changed events into your chosen iCloud calendar over CalDAV — the same protocol Apple Calendar uses.
- Attendee and organizer fields are stripped before upload, so iCloud never re-sends invitations to your events' guests.
- A state file tracks every synced event's last-modified timestamp and iCloud location; unchanged events are skipped, changed ones are updated in place, and events you delete in Google are deleted from iCloud (capped at 25 removals per run, so a feed glitch can never wipe your calendar).
- Events with no occurrence in the last 60 days are ignored (configurable). Recurring events are always kept while their recurrence is still live, no matter how old they are.
- The state file is committed back to your repo after each run — which also keeps the repo active, so GitHub never auto-disables the schedule.

## Setup

You need: a GitHub account, a Google calendar, and an iCloud account. About ten minutes.

1. **Use this template** — click "Use this template" → "Create a new repository" at the top of this page, and make your copy **Private**. (It must be private: the state file will contain your event UIDs and iCloud URLs.)
2. **Get your Google calendar's secret feed URL** — Google Calendar on the web → Settings → click your calendar → "Integrate calendar" → copy the **Secret address in iCal format** (it ends in `basic.ics`). Repeat for each calendar you want synced.
3. **Get an Apple app-specific password** — account.apple.com → Sign-In and Security → App-Specific Passwords → generate one (looks like `abcd-efgh-ijkl-mnop`). This is not your Apple ID password; you can revoke it anytime without affecting anything else.
4. **Add four secrets** in your new repo — Settings → Secrets and variables → Actions → New repository secret:
   - `ICLOUD_EMAIL` — your Apple ID email
   - `ICLOUD_APP_PASSWORD` — the app-specific password from step 3
   - `ICLOUD_CALENDAR_NAME` — the exact name of the iCloud calendar to sync into (create one in Apple Calendar first if you want, e.g. "Google")
   - `GCAL_ICAL_URLS` — the secret URL(s) from step 2, comma-separated if more than one
5. **Run it** — Actions tab → `gcal-icloud-sync` → Run workflow. Check the log; if the calendar name doesn't match, the error lists every calendar it found. After that it runs hourly on its own.

Want a no-risk first look? Edit the workflow's "Run sync" step to `python gcal_sync.py --dry-run` for one run — it reports everything it *would* do without touching iCloud.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `GCAL_ICAL_URLS` | (required) | Feed URLs, comma- or newline-separated |
| `ICLOUD_CALENDAR_NAME` | (required) | Target iCloud calendar, exact name |
| `WINDOW_DAYS` | `60` | Ignore events with no occurrence in the last N days |
| `DELETION_SYNC` | `true` | Set `false` to never delete anything from iCloud |
| `MAX_DELETIONS_PER_RUN` | `25` | Safety cap on deletions per run |
| `sync_ignore.txt` | — | Event titles/UIDs (one per line) the sync must never touch |

## Files

- **gcal_sync.py** - The sync engine
- **.github/workflows/sync.yml** - Hourly schedule + state commit-back
- **.github/workflows/test.yml** - Unit tests on every push
- **gcal_sync_state.json** - Per-event sync state (auto-maintained)
- **sync_ignore.txt** - Opt-out list for events you manage manually
- **tests/** - Window-filter, grouping, and parsing tests

## About

- Built a stateless-infrastructure calendar sync that runs entirely on GitHub Actions' free tier, persisting state by committing a JSON file back to its own repository after each run.
- Implemented CalDAV interop against iCloud's quirks discovered in production: recurring-event components grouped per UID into single uploads, `If-Match` ETag retry on 412 responses, and direct URL probing across all account calendars when iCloud rejects REPORT-by-UID queries.
- Demonstrates defensive sync design — 60-day occurrence windowing with RRULE expansion, deletion propagation behind a feed-failure check and a hard per-run cap, and an ignore list so manual calendar edits are never overwritten.

## Built With

Python | CalDAV | icalendar | GitHub Actions

## Troubleshooting and security

See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for the common failure modes (412 errors, revoked passwords, schedule delays) and [SECURITY.md](SECURITY.md) for exactly what this tool can and cannot access with your credentials.
