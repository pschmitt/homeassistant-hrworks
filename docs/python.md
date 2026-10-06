# Shared Python library

The installable package imports as `hrworks`. It is the same source bundled under
`custom_components/hrworks/hrworks` for HACS installations. Home Assistant adds
only its lifecycle listener, native flows, entities and repairs; SSH, the worker
protocol, authentication and TOTP are shared with the CLI. No Home Assistant or
Rich imports are needed to import the core library.

```python
import asyncio
from hrworks import WorkerClient, LocalTransport, authenticate


async def main():
    settings = {"company_id": "example", "username": "employee", "password": "..."}
    # Resolve secrets from your credential provider, never hardcode production secrets.
    transport = LocalTransport(["hrworks", "worker", "--stdio"])
    async with WorkerClient(settings, transport=transport) as client:
        result = await authenticate(client, settings)
        if result.get("mfa_required"):
            await client.mfa(input("Current code: "))
        account = await client.account()
        day = await client.day("2026-09-01")
        proposal = await client.record(
            {
                "start": "2026-09-01T08:00:00+02:00",
                "end": "2026-09-01T12:00:00+02:00",
                "type": "working_time",
                "dry_run": True,
            }
        )
        print(account, day, proposal)


asyncio.run(main())
```

For SSH, omit the explicit transport and provide `ssh_host`, `ssh_username`,
`ssh_key_path`, `ssh_known_hosts` (verified known_hosts text), and optionally
`ssh_port` and `worker_command`. Without explicit known-host text, the shared
transport reads `~/.ssh/known_hosts`; it never trusts an unknown key implicitly.

Methods: `health`, `login(force=False)`, `mfa`, `snapshot`, `account`, `day`,
`record`, `logout`, and `close`. `snapshot` takes `calendar_past_days`,
`calendar_future_days`, `include_sickness`, and `include_pending`. `record`
defaults to preview on the worker; actual submissions require `dry_run=False` and
worker permission. Each request is serialized and bounded. No request is replayed
by the transport. Lost/malformed replies to submitted writes raise
`WorkerError("write_uncertain")`; fixed error codes omit credentials and portal
captures. CLI and HA separately attach their native authentication UX and permit
one read recovery, never an automatic write retry.

Helpers include `authenticate`, `complete_mfa`, `secret_settings` and RFC 6238
`totp_code`. The optional `totp_uri` stays in the caller; only generated codes
reach the worker. `hrworks.models` provides interval parsing, numeric-minute
formatting, CSV validation and RFC 5545 calendar export. These helpers and the
protocol can be tested with fake transports without opening a browser.
