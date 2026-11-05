# HR WORKS for Home Assistant

- This repository includes the Home Assistant integration and a Python library/CLI for work-time balances, leave, and calendar data.
- Employee login only. Keep the browser automation in browser-daemon run scripts, separate from Home Assistant.
- Keep secrets, cookies, portal captures and personal HR data out of git and logs.
- Never test writes against real working times without explicit instruction.
- Preserve separate intervals, their types and comments. Never replace overlapping entries.
- Use native HA config, options, reauth, reconfigure and repair flows.
- Version releases with semver (`9.2.0`): keep `pyproject.toml`, `manifest.json`, `nix/package.nix`, `diagnostics.py` and `CHANGELOG.md` in sync, and tag the release with the bare version. Run Ruff formatting and linting for Python changes.
- Make focused commits on main; do not open pull requests unless requested.

## CI and tests

- Run `python -m unittest discover -s tests -v` and `python -m unittest discover -s tests/ha -v` for the unit and Home Assistant tests.
- Run `npm ci && npm test` in `tests/js` for the browser-daemon run scripts (`scripts/*.js`); they run against a synthetic, routed portal. On NixOS, set `PLAYWRIGHT_BROWSERS_PATH` to `nixpkgs#playwright-driver.browsers` (same version as the `playwright` pin) and `PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS=true`.
- Run `ruff check .`, `ruff format --check .`, and `python -m build` for Python or packaging changes; CI also smoke-tests the installed CLI and runs Hassfest.
- Before changing CI, check [pschmitt/ci-workflows](https://github.com/pschmitt/ci-workflows) for reusable components and preserve this repository's existing checks.
