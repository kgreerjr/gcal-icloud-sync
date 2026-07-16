# Security

Plain answers about what this tool touches, because you're trusting it with calendar credentials.

## What the credentials can do

An Apple **app-specific password is not scoped** — it authenticates as your full Apple ID for legacy-protocol services (CalDAV calendars, CardDAV contacts, IMAP mail on some accounts). This tool only ever talks to `caldav.icloud.com` and only reads calendar lists, reads/writes events, and deletes events it previously created — you can audit that in `gcal_sync.py`, it's one file. But you should understand the password itself could do more, which is why it belongs in GitHub's encrypted secrets and nowhere else. Revoke it anytime at account.apple.com with zero side effects beyond stopping the sync.

The Google side is read-only by construction: a secret iCal URL is a bearer link that exposes a read-only feed of one calendar. Anyone with the URL can read that calendar's events — treat it like a password (that's why it's a secret, not a workflow variable). If it ever leaks, reset it in Google Calendar settings ("Reset" next to the secret address) and update the secret.

## Why your repo must be private

`gcal_sync_state.json` is committed to your repo after each run. It contains event UIDs and iCloud object URLs — not event titles or contents, but real identifiers from your personal calendar. GitHub Actions logs also print event titles. A public fork of this template would publish both.

## What the workflow has access to

The sync job runs with `permissions: contents: write` on your repo only — enough to commit the state file back, nothing else. GitHub automatically masks your secrets in run logs. No third-party actions are used beyond `actions/checkout` and `actions/setup-python` (both GitHub-maintained).

## What this tool never does

- Never sends event data anywhere except from Google's feed to your iCloud calendar
- Never sends invitations (attendee fields are stripped before upload)
- Never deletes more than `MAX_DELETIONS_PER_RUN` events per run, and never deletes anything if a feed failed to load
- Never touches iCloud calendars other than reading their names/probing for moved events, and writing only to the one you named

## Reporting

Found a vulnerability or a way this tool leaks data? Open a GitHub issue (omit sensitive details) or contact the maintainer through the profile at github.com/kgreerjr.
