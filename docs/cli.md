# HR WORKS employee CLI

Regular employee login, optional TOTP, and the same browser adapter as Home Assistant.
No company API key is needed. The commands are `hrworks` and `hr-works`.

## Install with uv

```bash
uv tool install git+https://github.com/pschmitt/homeassistant-hrworks.git
hrworks --help
```

Or run without a permanent installation:

```bash
uvx --from git+https://github.com/pschmitt/homeassistant-hrworks.git hrworks --help
```

A local checkout also works with `uv run hrworks`, `uv tool install .`, or
`uvx --from . hrworks`. The wheel contains the library, CLI and worker. This is a
Git installation; publication to PyPI is not required.

Nix users can enable `programs.hrworksWorker.enable`. The module installs both
CLI names and configures the `hrworks worker` subcommand; Bash, Zsh and Fish
completions are packaged.
The Nix worker's `enableWrites` remains the deployment-level write permission.

## Browser and connection

Use a running Chromium with a CDP endpoint. The CLI starts a private worker
process with `hrworks worker` and an isolated browser context; it never closes the shared browser.
The default CDP endpoint is `http://127.0.0.1:9222`. Cookie state is private to the
worker's OS user, shared with HA when they use that same worker user.

Local worker:

```bash
hrworks config init --rbw-entry hrworks.de --cdp-url http://127.0.0.1:9222
hrworks doctor
hrworks auth login
```

SSH worker:

```bash
hrworks --profile work config init --transport ssh --ssh-host browser-host \
  --ssh-username employee --ssh-key ~/.ssh/id_ed25519 --rbw-entry hrworks.de
hrworks --profile work doctor
hrworks --profile work auth login
```

SSH reads `~/.ssh/known_hosts` and uses strict verification. To supply verified
keys explicitly, use `config init --known-hosts <file>`. It never accepts a host
key automatically. `--worker` selects the remote worker command or local worker command.

The optional `rbw` provider reads the username, password, TOTP URI, and a custom
field named `Company ID` into memory. Unlock rbw first. If the entry lacks a
company ID, set it with `config init --company-id ...`. Login can also prompt for
missing credentials when both input and output are terminals.

## Output

The default is colorful, aligned TSV in terminals, with bold column headings and
no box borders. Pipes receive literal TSV without ANSI codes. Embedded tabs and
newlines are quoted using CSV rules with a tab separator. Rich markup in portal
text is treated as literal text.

`--json` emits a single JSON document, including structured errors. Global
switches can appear before or after the subcommand. `--no-color` and the standard
`NO_COLOR` environment variable disable colors. Durations in TSV use `H:MM`;
JSON metrics retain numeric minutes. `--non-interactive` disables prompts.

```bash
hrworks balance
hrworks balance --json
hrworks times list                 # Current month
hrworks times list --date 2026-09-01 | tsvtool pretty
hrworks calendar list --kind leave --json
```

## Commands

| Command | Function |
|---|---|
| `doctor` | Worker protocol, browser connectivity and write capability |
| `snapshot` | Full HA snapshot; calendar windows, pending and sickness options |
| `balance` | Monthly work/target, carryover, credits and total balance, with period/basis |
| `status` | Today's credited work, target, balance and clocked-in state |
| `auth login [--force]` | Employee login and optional TOTP; force validates fresh credentials |
| `auth login --code-stdin` | Read a one-time MFA code from stdin without saving it |
| `auth status` | Verify or recover the saved employee session |
| `auth logout` | Delete employee browser cookies and local profile association |
| `times list [--date YYYY-MM-DD]` | Read separate intervals, including open intervals |
| `times list --month YYYY-MM` | Read every day of a month |
| `times types` | List supported working-time type keys |
| `times record --date ... --start ... --end ...` | Preview a completed interval |
| `times import FILE.csv` | Validate and preview every separate CSV interval |
| `calendar list [--from ... --to ... --kind ... --pending]` | Leave and sickness events |
| `calendar export FILE.ics` | Export leave/sickness as all-day iCalendar events |
| `config init` | Create or update local/SSH/vault/input-timezone settings |
| `config show` | Effective settings with secrets redacted |
| `config list` | Named profiles, transport and session/provider flags |
| `config secret password` | Save a password using a masked prompt or `--stdin` |
| `config secret totp_uri` | Save an `otpauth://totp/...` URI; `--clear` removes it |
| `config remove NAME` | Remove profile configuration after confirmation |
| `worker` | Run the JSON-line browser worker with CDP or Steel |
| `completion SHELL` | Completion script for bash, zsh, fish, powershell or pwsh |

`times list --month` reads days serially through the portal and can take several
minutes. Calendar start/end coverage is at most 730 days in either direction.
The CLI exposes the full functionality of the HA integration; unsupported portal
features such as payroll downloads or submitting leave requests are not invented.

## Working-time submissions

Previews are the default. Saving requires `--save` and terminal confirmation,
or the explicit combination `--save --yes` for scripts. A worker must also allow
writes: Nix `enableWrites = true`, or a local profile created with
`config init --enable-writes`. HA has its own additional write option.

```bash
hrworks times record --date 2026-09-01 --start 08:00 --end 12:00
hrworks times record --date 2026-09-01 --start 13:00 --end 17:00 \
  --type doctors_appointment --save --yes --json
```

`--start` and `--end` accept local `HH:MM` or ISO timestamps with offsets. Local
input defaults to Europe/Berlin; `config init --timezone ...` changes input
interpretation. The HR WORKS adapter currently uses Europe/Berlin business days.
Ambiguous/nonexistent DST wall times require explicit offsets. Seconds are
rejected rather than rounded. Future, unfinished, cross-day and overlapping
intervals are rejected. Existing entries are never overwritten. Exact duplicates
return `already_exists` and are not submitted again.

CSV import uses `date,start,end` and optional `type,comment` columns:

```csv
date,start,end,type,comment
2026-09-01,08:00,12:00,working_time,Morning
2026-09-01,13:00,17:00,working_time,Afternoon
```

```bash
hrworks times import intervals.csv
hrworks times import intervals.csv --save --yes --json
```

Every interval stays separate. All rows are validated and previewed before the
first submission. Imports are sequential, not transactional: if a later save
fails, earlier saves remain. JSON reports the successful results, failed index
and error; the CLI exits nonzero. A lost reply after submission is
`write_uncertain`. Review the affected day before retrying. Writes are never
automatically replayed; read operations have one authentication recovery attempt.

## Configuration and secrets

Default file: `$XDG_CONFIG_HOME/hrworks/config.toml`, or
`~/.config/hrworks/config.toml`. Writes are atomic and mode `0600`.
`--config` / `HRWORKS_CONFIG` selects another file;
`--profile` / `HRWORKS_PROFILE` selects a named account.

Environment overrides: `HRWORKS_COMPANY_ID`, `HRWORKS_USERNAME`,
`HRWORKS_PASSWORD`, `HRWORKS_TOTP_URI`, `HRWORKS_RBW_ENTRY`,
`HRWORKS_SSH_HOST`, `HRWORKS_SSH_PORT`, `HRWORKS_SSH_USERNAME`,
`HRWORKS_SSH_KEY`, `HRWORKS_WORKER`, and `HRWORKS_CDP_URL`.
Secrets can come from the environment, rbw, masked prompts, or stdin. Passwords,
seeds and one-time codes are never accepted as command-line arguments. Credentials
resolved from rbw or the environment are not copied into profile configuration.
Explicit `config secret` values are stored privately, as requested. Protect that
file and the worker cookie directory as you would your login credentials.

## Exit status and shell completion

- `0`: success, including previews and duplicates.
- `1`: worker, configuration, IO, or failed batch operation.
- `2`: invalid command-line usage.
- `3`: employee authentication/MFA required or rejected.
- `4`: uncertain working-time submission; manual review required.

```bash
hrworks completion zsh > ~/.config/zsh/completions/_hrworks
hrworks completion bash > hrworks-completion.bash
hrworks completion fish > ~/.config/fish/completions/hrworks.fish
```

## Nix and NixOS

The locked flake supports `x86_64-linux` and `aarch64-linux`:

```console
nix run github:pschmitt/homeassistant-hrworks -- --help
nix run github:pschmitt/homeassistant-hrworks -- balance
nix profile add github:pschmitt/homeassistant-hrworks#hrworks
```

For a declarative NixOS installation, add the input and CLI module:

```nix
# flake.nix
inputs.hrworks.url = "github:pschmitt/homeassistant-hrworks";
inputs.hrworks.inputs.nixpkgs.follows = "nixpkgs";

# In the workstation's modules list:
hrworks.nixosModules.cli

# In its configuration:
programs.hrworks.enable = true;
```

Alternatively, put `hrworks.packages.${pkgs.stdenv.hostPlatform.system}.hrworks`
in `environment.systemPackages` or Home Manager's `home.packages`.
Bash, Zsh and Fish completions are included. `nixosModules.worker` configures
the defaults for `hrworks worker`; `nixosModules.default` imports the same module.

Installations are declarative; employee secrets and browser sessions stay in
private runtime storage. A workstation can use `config init --transport ssh`
to connect to an existing worker without running Chromium locally.

`times list` defaults to the current month through today, skipping empty future days. Use `--date` for one day or `--month YYYY-MM` to read a complete month.
