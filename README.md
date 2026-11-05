<p align="center">
  <img src="icon.png" width="96" alt="HR WORKS for Home Assistant icon">
</p>

<p align="center">
  <a href="https://my.home-assistant.io/redirect/marketplace_repository/?owner=pschmitt&amp;repository=homeassistant-hrworks&amp;category=integration"><img src="https://my.home-assistant.io/badges/marketplace_repository.svg" alt="Open this integration in Home Assistant Marketplace"></a>
</p>

<h1 align="center">HR WORKS for Home Assistant</h1>

<p align="center">Track working-time balances, leave and calendar events with your employee login and a browser-daemon profile.</p>

## Features

- **Time accounts:** Show worked and target hours, monthly balances and carryover. See [Integration](docs/integration.md).
- **Absences and calendar:** Track vacation, sickness and upcoming leave with approval status and half-day annotations. See [Integration](docs/integration.md).
- **Working-time actions:** Preview an interval before submitting it; writes require an explicit option. See [Integration](docs/integration.md#enter-working-times).
- **Employee profiles:** Keep separate Home Assistant entries, browser profiles and settings for each employee.
- **Authentication:** Use direct credentials or optional rbw vault access, with persistent or throwaway browsers and optional node groups. See [Authentication](docs/authentication.md).
- **Command-line access:** Read balances and calendar events or preview time entries with `hrworks`. See [CLI](docs/cli.md).

## Install

Requires Home Assistant 2026.11 or later. Use the Marketplace badge above to
add this repository as a custom integration, or add it as a HACS custom
repository of type **Integration**. Install and restart Home Assistant.
The badge does not imply a public catalog listing.

## Quick start

1. Set up the Browser Daemon integration or a browser-daemon node with runs enabled.
2. Add **HR WORKS** under **Settings → Devices & services → Add integration**.
3. Keep `ha-hrworks` to create a persistent profile on first use, select another
   name, or clear the field for a throwaway browser. Enter direct employee
   credentials or select rbw; complete any authenticator challenge in the flow.
4. Inspect the new time-account entities, or add them to a dashboard with the
   [example configuration](examples/dashboard.yaml).

Direct employee passwords and any entered cluster secret are stored in Home
Assistant. Protect its backups and the browser profile.

## Documentation

| Topic | Description |
| --- | --- |
| [Integration](docs/integration.md) | Entities, setup details, repairs, authentication and working-time actions. |
| [Authentication](docs/authentication.md) | Browser placement, optional rbw credentials and MFA. |
| [CLI](docs/cli.md) | Terminal commands and options. |
| [Python](docs/python.md) | Library usage and integration notes. |
| [Dashboard example](examples/dashboard.yaml) | Native tiles, balances and calendars. |
| [Changelog](CHANGELOG.md) | Changes by release. |

## License

[MIT](LICENSE).
