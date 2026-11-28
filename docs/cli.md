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
`uvx --from . hrworks`. The wheel contains the library and CLI and depends on
`browser-daemon-client`. This is a Git installation; publication to PyPI is not required.

Nix users can enable `programs.hrworks.enable`. The module installs both CLI names;
Bash, Zsh and Fish completions are packaged.

## Browser and connection

The CLI runs the portal scripts on a [browser-daemon](https://github.com/pschmitt/browser-daemon)
node through its run API (the same code as the Home Assistant integration). The
profile's browser keeps the HR WORKS login (the login form's "Keep me logged in" is
ticked), so later invocations reuse it; the CLI signs in with your saved credentials
when the session has expired. Runs must be enabled on the node (`--run-node`).

| Setting | Default | Override |
| --- | --- | --- |
| browser-daemon URL | `http://127.0.0.1:3001` | `config init --browser-daemon-url`, `BROWSER_DAEMON_URL` |
| Cluster secret file | `/run/secrets/browser-daemon/cluster-secret` | `--secret-file`, `BROWSER_DAEMON_SECRET_FILE`, `config init --cluster-secret-file` |
| Browser profile | `hrworks` | `config init --browser-profile`, `HRWORKS_BROWSER_PROFILE` |

The cluster secret is read from its file for each run and never printed or saved in the
profile (only the file path can be). Profiles of older versions may still contain
`transport`, `ssh_*`, `worker_command`, `cdp_url` or `state_dir`; those keys are ignored
and dropped the next time the profile is saved.

```bash
hrworks config init --rbw-entry hrworks.de
hrworks --profile work config init --browser-daemon-url https://browser.example.org \
  --cluster-secret-file ~/.config/browser-daemon/secret --rbw-entry hrworks.de
hrworks doctor
hrworks auth login
```

The optional `rbw` provider reads the username, password, TOTP URI, and a custom
field named `Company ID` or `company` into memory. Unlock rbw first. If the entry
lacks a company ID, set it with `config init --company-id ...`. Login can also prompt for
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
| `doctor` | browser-daemon, profile browser and write capability |
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
| `times record --raw < table.txt` | Preview what HR WORKS' AI assistance proposes for a pasted time table |
| `times import FILE.csv` | Validate and preview every separate CSV interval |
| `calendar list [--from ... --to ... --kind ... --pending]` | Leave and sickness events |
| `calendar export FILE.ics` | Export leave/sickness as all-day iCalendar events |
| `config init` | Create or update browser-daemon, vault and input-timezone settings |
| `config show` | Effective settings with secrets redacted |
| `config list` | Named profiles, browser profile and session/provider flags |
| `config secret password` | Save a password using a masked prompt or `--stdin` |
| `config secret totp_uri` | Save an `otpauth://totp/...` URI; `--clear` removes it |
| `config remove NAME` | Remove profile configuration after confirmation |
| `completion SHELL` | Completion script for bash, zsh, fish, powershell or pwsh |

`times list --month` reads days serially through the portal and can take several
minutes. Calendar start/end coverage is at most 730 days in either direction.
The CLI exposes the full functionality of the HA integration; unsupported portal
features such as payroll downloads or submitting leave requests are not invented.

## Working-time submissions

Previews are the default. Saving requires `--save` and terminal confirmation,
or the explicit combination `--save --yes` for scripts. The profile must also allow
writes (`config init --enable-writes`; the default is off). HA has its own write option.

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

### AI assistance (`--raw`)

`times record --raw` reads a time table or free text from stdin and sends it to the portal's
"AI assistance" dialog, like typing it there by hand:

```bash
hrworks times record --raw <<< "$(tw::summary week)"               # preview only
hrworks times record --raw --save <<< "$(tw::summary week)"        # confirm, then save
```

Rows shaped `DATE START END [TIME TOTAL] [COMMENT]` stay separate intervals (lunch breaks are
never merged), seconds are dropped, unfinished rows (`END` of `N/A`) are skipped and rows whose
comment names a doctor's appointment get that type. The proposal must match the rows exactly,
or nothing is saved (`proposal_mismatch`). With `--save` the previewed proposal is what gets
entered and then verified on the portal; a row the portal reports as existing aborts the save.
Free text is previewed as proposed, with nothing to compare against.

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
`BROWSER_DAEMON_URL` (or `HRWORKS_BROWSER_DAEMON_URL`), `BROWSER_DAEMON_SECRET_FILE`
and `HRWORKS_BROWSER_PROFILE`.
Secrets can come from the environment, rbw, masked prompts, or stdin. Passwords,
seeds and one-time codes are never accepted as command-line arguments. Credentials
resolved from rbw or the environment are not copied into profile configuration.
Explicit `config secret` values are stored privately, as requested. Protect that
file and the browser-daemon profile as you would your login credentials.

## Exit status and shell completion

- `0`: success, including previews and duplicates.
- `1`: browser-daemon, configuration, IO, or failed batch operation.
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
Bash, Zsh and Fish completions are included. `nixosModules.default` is the same module
as `nixosModules.cli`. There is no worker module any more: the browser runs in
browser-daemon.

Installations are declarative; employee secrets and browser sessions stay in
private runtime storage.

`times list` defaults to the current month through today, skipping empty future days. Use `--date` for one day or `--month YYYY-MM` to read a complete month.
