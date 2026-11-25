# Changelog

## Unreleased

- Reads that fail together after an expired login now log in once: one login at
  a time, reused by every read that failed before it, and no other operation runs
  in the portal tab between the login and its MFA step. Concurrent calendar reads
  used to log in over each other and expire the pending MFA (`session_expired`).
- The calendar services no longer log in again per call once the automatic
  recovery has given up; they ask for the repair instead.
- The development shell provides Home Assistant, so `tests/ha` runs in it.
- The login follows the portal's waiting page ("Gleich geht's weiter") after the
  password to the second factor it then asks for. It used to take the waiting page
  for a finished login, so the next read hit the second factor and failed with
  `session_expired` or `unsupported_mfa`. The login may take up to 150 seconds.
- The second-factor selection waits for its page to load and clicks Authenticator
  again (three tries) when a click lands before the page reacts; a lost click used
  to end the login with `login_timeout`.

## 9.2.0

- Days off without a working-times row read as days without target instead of
  stopping the Work days history at a free weekday; weeks over New Year are found.
- Rejected logins report `invalid_auth`, unanswered logins report `login_timeout`
  (retried), and unsupported second factors report `unsupported_mfa`.
- `times record --raw` enters times through HR WORKS' AI assistance. The browser
  session ends after each refresh.

## 9.1.0

- The `hrworks` CLI returns on browser-daemon, without the old `worker` command
  or transport options; `programs.hrworks` remains available.

## 9.0.0

- The SSH worker and `programs.hrworksWorker` are removed. Browser-daemon is the
  only backend (config entry version 2).
