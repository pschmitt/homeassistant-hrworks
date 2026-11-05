# Integration

## Entities

| Entities | Details |
| --- | --- |
| Time account | Worked and target hours, monthly balance, carryover and total balance |
| Daily time | Credited work, target and balance where the portal exposes them |
| Vacation | Available and approved days |
| Work days | Worked weekdays of the last 30 days; a `days` attribute lists each weekday's status (worked, leave, sick, holiday, absent, unknown) with worked and target hours |
| Calendar | One account calendar with leave and sickness, approval status and half-day annotations |
| Status | Clocked in (with `since` and today's intervals), on leave and on sick leave (current absence's `start`/`end`/`days`/`status`, plus the `next_*` one) |
| Controls | Refresh button and a working-time action with a preview mode |

One service device per employee. English and German translations. Numeric hours for
graphs and calculations, with a readable `formatted` attribute such as `-1:30`.
Different employees can have their own entries, browser profiles and settings.

## How it works

```mermaid
flowchart LR
    HA[Home Assistant] -->|HTTPS · POST /api/run| BD[browser-daemon node]
    BD -->|Playwright script in the profile's browser| HR[HR WORKS]
```

The portal logic runs as Playwright scripts
(`custom_components/hrworks/hrworks/scripts/*.js`) on a
[browser-daemon](https://github.com/pschmitt/browser-daemon) node, through its run API.
Home Assistant installs only `browser-daemon-client` (which needs just `aiohttp`).
Runs for a profile go to the node that has it.

- **Connection:** reuse the Home Assistant Browser Daemon integration, or enter
  a node URL and cluster secret. Runs must be enabled on the selected node
  (`--run-node`). An optional node group restricts browser placement. The cluster
  secret can start runs, so treat it like the employee password.
- **Profile:** the integration creates `ha-hrworks` on first use. Existing profile
  names are preserved; clear the field for a throwaway browser. Use a dedicated
  employee profile: forced login clears its `hrworks.de` cookies.
- **Authentication:** use direct employee credentials and an optional TOTP URI,
  or select the Home Assistant rbw integration for fresh vault credentials.
  See [Authentication](authentication.md) for placement, vault policy and MFA.
- **One tab:** the portal session lives in the tab that signed in, so the integration keeps
  that one tab (`keep_open`) and runs every operation in it. A TOTP challenge is finished
  in the same tab; a failed login or a logout closes it, and a tab which vanished counts as
  an expired session (the automatic recovery then signs in again in a new tab). After a
  Home Assistant restart the profile's open HR WORKS tab is taken over. An abandoned
  challenge expires after 10 minutes.
- **Secrets:** credentials and one-time codes travel in the run's `args` only. They are
  not part of the scripts and browser-daemon does not log them.
- **Writes:** working-time entries are previews by default, and a real write needs
  **Allow working-time submissions** in the options. A write which may have reached the
  portal (timeout, connection loss) is never replayed and raises the *uncertain write*
  repair.
- **Day history** reads open the page again for every day, so they are slower than a
  single snapshot.

## Command-line tool

`hrworks` / `hr-works` reads the same data from a terminal through browser-daemon
(`balance`, `status`, `snapshot`, `times list`, `times record` previews, `calendar list`,
...). See the [CLI guide](cli.md) and the [library notes](python.md). With Nix:
`programs.hrworks.enable = true` (import `nixosModules.cli`, or `nixosModules.default`).

## Upgrading from version 8 (SSH worker)

Version 9 removes the SSH worker and its NixOS module (the `hrworks` CLI stays and now uses browser-daemon; its old transport options are ignored). Entries which
already used browser-daemon migrate by themselves (the old SSH keys are dropped from
the entry). An entry which still used the SSH worker keeps its employee login but fails
to load with a Repairs notice: open the integration, choose **Reconfigure** and enter
the browser-daemon connection and profile. Entity IDs are kept.

## Configuration details

Requires Home Assistant 2026.11 or later for conditional configuration fields.
Enter the browser-daemon URL, cluster secret and profile, or leave URL and secret
empty to use the Browser Daemon integration. Enter your employee company ID and
user ID, then choose direct password/TOTP credentials or an optional rbw account
item. Only fields for the selected credential source are shown. See
[Authentication](authentication.md) for setup and permissions. One-time codes
are never stored.

## Settings and repair

- **Reconfigure:** change the browser-daemon connection, device name, company, username, password or TOTP URI. Blank secret fields keep the saved values; use **Remove saved TOTP URI** to clear it. Account changes preserve entity IDs and use a fresh login.
  A blank employee password keeps the existing value. Changing employees requires a new entry.
- **Options:** update interval (5 minutes–1 day), calendar history and lookahead
  (up to two years each), pending leave, sickness visibility and time-entry permissions.
- **Repairs:** expired employee sessions use fresh rbw credentials when selected,
  or the saved direct password and TOTP URI. Enter a fresh authenticator code if
  needed; change the credential source through Reconfigure. Browser failures and
  portal changes have specific guidance.
- **Diagnostics:** contain metric names, counts and operational status; exclude
  credentials, company/user IDs, the cluster secret, event details and HR values.

The refresh button also retries after a browser outage. Successful updates clear the
relevant repairs. A calendar only covers the configured window; its `coverage`
attribute gives the exact bounds. Past days for `Work days` are read a few per update
and cached, so the 30-day window fills in over the first hour. Absence `end` attributes are the last day, as the
portal shows it. Cancelled or rejected leave is never treated as approved absence.

## Diagnostic sensors

Each employee device also carries diagnostic entities (category *Diagnostic*) for
telling a slow or failing refresh from a healthy one:

| Entity | Meaning |
| --- | --- |
| Update duration | Seconds the whole refresh took in HA (browser-daemon runs, recovery). |
| Worker duration | Seconds the snapshot run took; the steps (`account_seconds`, `events_seconds`, `today_seconds`) are attributes. |
| Browser session | The browser-daemon session the run used, with backend, endpoint, browser version and the number of browser connections as attributes. |
| Browser connect duration, Browser version | Same data as single values (disabled by default). |
| Next update | When the next scheduled refresh runs (shorter while retrying). |
| Last error | `none` or the error code. |
| Consecutive failures, Failed updates, Updates, Re-logins | Counters since HA started; *Re-logins* counts automatic recoveries with the saved TOTP. |
| Calendar events | Events in the calendar window of the last snapshot. |
| Update problem | Binary sensor, on while the last refresh failed. |

No personal HR data is in them. The browser endpoint is only host and port.

## Reporting periods

HR WORKS may expose the newest available month before it creates an account for
the current month. Every time-account sensor includes `period`, `current_month`
and `balance_basis`. The latest previous month is labelled as such, never silently
presented as the current month.

Balances come from the detailed account, including leave deductions and corrections.
The current month's “to the previous day” balance takes precedence when exposed.
Missing or unparseable values are **unavailable**, never synthesized as zero.

Half-day leave stays an all-day calendar entry with a half-day description. No artificial clock times are invented for an absence.

## Enter working times

`hrworks.record_working_time` handles **one completed interval**. Include an explicit
timezone offset, and send separate calls for morning and afternoon work.

```yaml
action: hrworks.record_working_time
data:
  start: "2026-10-02T08:30:00+02:00"
  end: "2026-10-02T12:00:00+02:00"
  type: working_time
  comment: "Morning work"
  dry_run: true
response_variable: preview
```

Previews validate the interval and inspect existing entries without adding an entry.
To save, enable time entry in the integration options, then explicitly set `dry_run: false`. Writes are disabled by default.
The action refuses incomplete, future, overlapping, or cross-midnight intervals.
Use minute precision: seconds are rejected rather than silently rounded.
Supported types include ordinary work, doctor appointments, business errands and training.
Available types and edit permissions still depend on your employer's portal configuration.

A successful save is checked by reopening the day. An uncertain result raises a
repair; check HR WORKS before resubmitting. Neither browser-daemon nor HA automatically
retries a write after a timeout. No historical gaps are automatically filled.

## Dashboard

An example using native tiles, a balance graph and calendars is in
[`examples/dashboard.yaml`](../examples/dashboard.yaml). Adjust entity IDs to your device name.
The dashboard needs no custom frontend cards.

## Updating

HACS updates the integration; browser-daemon is updated separately. Employee sessions stay in the browser-daemon profile.

This is an independently maintained custom integration. HR WORKS does not provide
or support the browser protocol used here. Portal layout changes may require a
script update. This integration does not create leave or sickness requests.

## Authentication and maintenance

For direct credentials, an optional `otpauth://totp/...` URI can be entered during account
setup, Reconfigure, or an MFA challenge in Repairs. Use the URI behind your
authenticator’s QR code. Six-digit SHA1, SHA256 and SHA512 TOTP URIs are accepted.
The seed is stored alongside the employee password in Home Assistant, never
prefilled into forms, included in diagnostics, or sent to browser-daemon.
Only the generated one-time code is sent. Protect Home Assistant backups
as you would your password manager.

With a URI saved, an expired session gets a fresh login and one snapshot retry.
If the login response times out, HA retries the login after 1, 5, 15 and 30 minutes.
After five failed attempts, automatic login pauses until the custom Repairs flow is
completed. Authentication errors other than a login-response timeout go straight
to Repairs. A browser or connection failure discards the failed run.
Home Assistant retries the read up to three times after the first attempt,
waiting three, six and then ten seconds between attempts, before scheduling recovery
and polling every minute while those failures persist. Successful updates restore
the configured polling interval and clear the browser repair. Working-time
submissions are never automatically replayed; a browser failure during a
submission is reported as uncertain so you can check the recorded times before
trying again.

The adapter discovers year/month options, follows navigation links, reads editor
controls through their labels, and discovers working-time choices by displayed
labels rather than option IDs. Label whitespace, trailing colons, typographic
apostrophes and regenerated control IDs are tolerated. English and German labels
are supported. Unknown meanings, ambiguous rows and changed structural containers
fail visibly through Repairs rather than guessing or publishing fabricated data.

Browser integration remains dependent on HR WORKS’ private portal layout. It
cannot offer the stability of an official API. Authentication and coordinator regression tests run
against Home Assistant itself. All fixtures use invented data.

## Development

Tests need `browser-daemon-client` on `PYTHONPATH` (the `python/` directory of the browser-daemon repository), Node.js for the script checks and, for `tests/ha`, Home Assistant:

```sh
nix develop   # or: ln -s custom_components/hrworks/hrworks <dir>/hrworks; PYTHONPATH=.:<dir>:<browser-daemon>/python
python -m unittest discover -s tests -v
python -m unittest discover -s tests/ha -v
```
