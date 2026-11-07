# Authentication

These configuration forms require Home Assistant 2026.11 or later. Password
and TOTP fields appear for direct credentials; the vault item field appears
when an rbw integration is selected.

## Browser-daemon connection and placement

HR WORKS uses browser-daemon's Playwright run API. Select the Browser Daemon
integration to use; it provides the connection, so no URL or cluster secret is
stored here. Turn on the override to enter a URL and cluster secret instead,
which does not need that integration. Runs must be enabled on the selected node.

New setups default to the dedicated `ha-hrworks` profile, created by the
integration before its first run. Existing configured names remain unchanged.
Clear the profile field for a throwaway browser: one session is retained across
login and MFA, then stopped after the refresh; cookies are not retained.

The optional **Browser Daemon node group** restricts placement to nodes in that
group. An idle existing profile can move there without overwriting another copy. An active profile outside the group, duplicate copies,
incomplete inventory, or unavailable group capacity stops the operation;
there is no fallback outside the group. Group placement requires a daemon
serving `POST /api/mesh/placement`. Leave the field empty for normal profile
routing.

## Direct employee credentials

rbw is optional. Leave both rbw fields empty and enter your company ID, user
ID, password and optional TOTP provisioning URI. The password and TOTP URI are
stored in the Home Assistant config entry; protect its backups accordingly.
Blank secret fields retain saved values. **Remove saved TOTP URI** clears a
saved URI. A manual authenticator code is never stored.

Six-digit SHA1, SHA256 and SHA512 TOTP URIs are supported. The seed stays in
Home Assistant; only a generated current code is sent to browser-daemon.

## Home Assistant rbw integration

Select an rbw integration and the employee account item below it. The list shows
the items rbw allows HR WORKS to read, by name; you can also enter an item ID,
and rbw asks you to allow it under Repairs. Leave the rbw integration empty to
enter a static password and TOTP URI instead. Grant that rbw
client access only to this item's `username`, `password` and current `totp`
fields. The username must match the configured employee. The company ID stays
in the HR WORKS entry.

Each authentication attempt reads the current username and password through
Home Assistant rbw's `async_get_field`; an MFA challenge reads the current
`totp` code. Password changes in the vault therefore take effect without
copying the replacement into HR WORKS settings. No password or TOTP seed from
rbw is stored in the HR WORKS config entry. Selecting rbw removes any previously
saved direct password and TOTP URI.

A locked vault, denied field or account mismatch stops authentication. The
integration does not fall back to a stale saved password. Unlock the vault or
correct its item/field policy, then retry the authentication repair. A manual
current code can finish MFA without being saved. HR WORKS uses password and
TOTP authentication, not Teams' Microsoft passkey flow.

## Reconfigure and repair

Use native **Reconfigure** to change the connection, profile, node group or
credential source. Switching from rbw back to direct credentials requires a
password because no vault password was retained. Reauthentication cannot
silently switch employees; add another integration entry for another account.

Expired sessions use the selected credential source for automatic recovery
when rbw or a saved TOTP URI is configured. Authentication failures surface a
Home Assistant repair. Working-time submissions are never replayed during
recovery. No real working-time writes are needed to test authentication.
