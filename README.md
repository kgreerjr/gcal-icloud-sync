# gcal-icloud-sync

A free one-way sync from Google Calendar into an iCloud calendar of your choice, running on GitHub Actions with no server, no OAuth app, and no subscription.

Paid services like CalendarBridge, SyncGene, and OneCal host this kind of sync for $5-15/month. The free open-source alternatives can't write into iCloud. This tool fills that gap: your Google events land in a real iCloud calendar where they behave like native Apple Calendar events (color-coding, widgets, Siri, sharing), and your credentials never leave your own GitHub repository.

## How It Works

- Every hour, a GitHub Actions job fetches your Google calendars' secret iCal feeds and uploads new or changed events into your chosen iCloud calendar over CalDAV, the same protocol Apple Calendar uses.
- Attendee and organizer fields are stripped before upload, so iCloud never re-sends invitations to your events' guests.
- A state file tracks each synced event's last-modified timestamp and iCloud location. Unchanged events are skipped, changed events are updated in place, and events you delete in Google are deleted from iCloud (capped at 25 removals per run, so a feed glitch can never wipe your calendar).
- Events with no occurrence in the last 60 days are ignored (configurable). Recurring events are always kept while their recurrence is still live, no matter how old they are.
- The state file is committed back to your repo after each run, which also keeps the repo active so GitHub never auto-disables the schedule.

## Setup

You need a GitHub account, a Google calendar, and an iCloud account. About ten minutes.

1. Click "Use this template" then "Create a new repository" at the top of this page, and make your copy **Private**. It must be private because the state file will contain your event UIDs and iCloud URLs.
2. Get your Google calendar's secret feed URL: Google Calendar on the web, Settings, click your calendar, "Integrate calendar", copy the **Secret address in iCal format** (it ends in `basic.ics`). Repeat for each calendar you want synced.
3. Get an Apple app-specific password at account.apple.com under Sign-In and Security. This is not your Apple ID password, and you can revoke it anytime without affecting anything else.
4. Add four secrets in your new repo under Settings, Secrets and variables, Actions:
   - `ICLOUD_EMAIL` - your Apple ID email
   - `ICLOUD_APP_PASSWORD` - the app-specific password from step 3
   - `ICLOUD_CALENDAR_NAME` - the exact name of the iCloud calendar to sync into
   - `GCAL_ICAL_URLS` - the secret URL(s) from step 2, comma-separated if more than one
5. Run it from the Actions tab (gcal-icloud-sync, Run workflow) and check the log. If the calendar name doesn't match, the error lists every calendar it found. After that it runs hourly on its own.

For a no-risk first look, edit the workflow's "Run sync" step to `python gcal_sync.py --dry-run` for one run. It reports everything it would do without touching iCloud.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `GCAL_ICAL_URLS` | (required) | Feed URLs, comma- or newline-separated |
| `ICLOUD_CALENDAR_NAME` | (required) | Target iCloud calendar, exact name |
| `WINDOW_DAYS` | `60` | Ignore events with no occurrence in the last N days |
| `DELETION_SYNC` | `true` | Set `false` to never delete anything from iCloud |
| `MAX_DELETIONS_PER_RUN` | `25` | Safety cap on deletions per run |

## Sync Now From Your iPhone (optional)

The hourly schedule is fine for most changes, but after adding an event you sometimes want the sync immediately. An iOS Shortcut can trigger a run in one tap.

1. On GitHub, create a fine-grained personal access token (Settings, Developer settings, Fine-grained tokens): Repository access limited to **your private sync repo only**, with the **Actions** permission set to Read and write. Nothing else.
2. In the Shortcuts app, create a new shortcut with a single "Get Contents of URL" action:
   - URL: `https://api.github.com/repos/YOUR_USERNAME/YOUR_REPO/actions/workflows/sync.yml/dispatches`
   - Method: POST
   - Headers: `Authorization` = `Bearer YOUR_TOKEN`, and `Accept` = `application/vnd.github+json`
   - Request Body: JSON with one Text field, `ref` = `main`
3. Name it something like "Sync Calendar" and add it to your Home Screen. Tapping it starts a run within seconds.

The token only allows triggering workflows on that one repo, so the damage ceiling if your phone is compromised is that someone syncs your calendar.

## Files

- **gcal_sync.py** - The sync engine
- **.github/workflows/sync.yml** - Hourly schedule and state commit-back
- **.github/workflows/test.yml** - Unit tests on every push
- **gcal_sync_state.json** - Per-event sync state, maintained automatically
- **sync_ignore.txt** - Opt-out list for events the sync must never touch
- **tests/** - Window-filter, grouping, and parsing tests

## About

- Built a serverless calendar sync that runs entirely on GitHub Actions' free tier, persisting state by committing a JSON file back to its own repository after each run.
- Implemented CalDAV interop against iCloud quirks discovered in production: recurring-event components grouped per UID into single uploads, If-Match ETag retry on 412 responses, and direct URL probing across all account calendars when iCloud rejects REPORT-by-UID queries.
- Demonstrates defensive sync design with occurrence windowing and RRULE expansion, deletion propagation behind a feed-failure check and a hard per-run cap, and an ignore list so manual calendar edits are never overwritten.

## Built With

Python | CalDAV | icalendar | GitHub Actions

See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for common failure modes and [SECURITY.md](SECURITY.md) for exactly what this tool can and cannot access with your credentials.
