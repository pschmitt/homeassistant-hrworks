# Changelog

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
