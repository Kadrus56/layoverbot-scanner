# layoverbot-scanner

The hourly AIMS eCrew flight-roster scan for [LayoverBot](https://github.com/Kadrus56/LayoverBot),
split into its own **public** repository for one reason: GitHub Actions
minutes are metered on a private repo and unmetered on a public one, and this
scan — run once an hour, every hour — is what was burning through them.

## What's here

- `scripts/ecrew_layovers.py` — logs into AIMS eCrew, reads the current
  flight/crew roster, and works out who is on a layover (foreign arrival, next
  departure from that city ≥10h away).
- `.github/workflows/scan.yml` — runs the scan hourly and delivers the
  resulting roster to the Oracle server over SSH, where the actual bot
  (private repo) reads it.

## What's not here

No credentials, no flight data, no crew data. `ECREW_USERNAME`,
`ECREW_PASSWORD`, `ORACLE_HOST`, `ORACLE_USER`, `ORACLE_SSH_KEY` all live in
this repo's own [Actions Secrets](../../settings/secrets/actions) — never in
code, never in a commit. The scan output (who's where, on what flight) exists
only for the duration of one workflow run and is shipped straight to Oracle;
it isn't stored in this repo.

This is a mirror of `scripts/ecrew_layovers.py` from the private LayoverBot
repo. The two are kept in sync by hand — this one is small and self-contained
on purpose (standard library + `playwright`, nothing else), so a diff between
the two catches drift on sight.
