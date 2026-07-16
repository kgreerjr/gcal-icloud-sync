# Troubleshooting

Every issue below was hit for real during development. Check the Actions run log first — the script narrates what it did to every event.

## "Calendar 'X' not found in iCloud"

The `ICLOUD_CALENDAR_NAME` secret must match the calendar's name exactly (case, spaces, everything). The error message lists every calendar name it found in your account — copy the right one from there into the secret.

## Authentication fails / 401

Your app-specific password was probably revoked. Apple revokes **all** app-specific passwords automatically whenever you change or reset your Apple ID password, and does not notify you. Generate a new one at account.apple.com → Sign-In and Security → App-Specific Passwords, update the `ICLOUD_APP_PASSWORD` secret, and re-run. (The failed workflow run already emailed you — that's your alert system.)

## 412 Precondition Failed

iCloud returns 412 in several unrelated situations, and the script already handles the known ones automatically: updates retry with an `If-Match` ETag, recurring events upload all their components together, and when iCloud's lookup-by-UID query itself 412s (it does this on some accounts), the script probes the event's likely URL directly across every calendar. If you still see a persistent `UID occupied in iCloud, leaving as-is` line, it means an event with that UID exists somewhere iCloud won't let us reach — usually a leftover from manually deleting "all future events" of a synced series in Apple Calendar. That's harmless; the sync marks it handled and moves on. If you want the event resynced fresh, delete it entirely (including past occurrences) in Apple Calendar, edit the event in Google to bump its modification time, and re-run.

## Events aren't appearing

- Check the run summary line: if they're counted in `outside the N-day window`, they're older than `WINDOW_DAYS` with no upcoming occurrence — that's by design.
- If they're counted as `ignored`, they match a line in `sync_ignore.txt`.
- If the feed fetch failed, Google's secret iCal URLs occasionally lag behind reality by a few minutes to an hour — Google caches feed output. A brand-new event may take a couple of runs to show up.

## The schedule seems late or skipped

Normal. GitHub Actions cron is best-effort — runs are frequently delayed by several minutes, occasionally skipped entirely under load. The workflow deliberately fires at :17 past the hour (top-of-hour is the most congested slot). If you need a sync right now, trigger it manually from the Actions tab, or set up a phone shortcut that POSTs to the workflow-dispatch API.

## The schedule stopped entirely

GitHub disables scheduled workflows after 60 days without repo activity — but this tool commits its state file back after every run that changes anything, which counts as activity, so this shouldn't happen while events are changing. If your calendar was completely static for two months, GitHub emails you before disabling; just re-enable from the Actions tab.

## Duplicate events in Apple Calendar

Check whether your Google account is *also* added directly to your iPhone/Mac (Settings → Calendar → Accounts) with the same calendar visible. You'd be seeing the native Google copy plus the synced iCloud copy. Either hide the Google calendar in the Calendar app's calendar list, or remove the account. The sync itself will not duplicate: updates go to the stored URL of the existing event.

## Guests got invitation emails from iCloud

They shouldn't — attendee stripping removes `ATTENDEE`/`ORGANIZER` before upload, precisely to prevent this. If you see it happen, file an issue with the run log (redact your URLs).

## I deleted an event in Apple Calendar and it came back

Expected: Google is the source of truth, and the sync recreates anything still live in the feed. To make a deletion stick, delete the event in **Google** (it will then be removed from iCloud automatically), or add its title or UID to `sync_ignore.txt` to make the sync leave it alone permanently.
