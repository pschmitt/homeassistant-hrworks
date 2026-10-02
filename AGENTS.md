# HR WORKS integration

- Employee login only. Keep the browser worker separate from Home Assistant.
- Keep secrets, cookies, portal captures and personal HR data out of git and logs.
- Never test writes against real working times without explicit instruction.
- Preserve separate intervals, their types and comments. Never replace overlapping entries.
- Use native HA config, options, reauth, reconfigure and repair flows.
- Version releases with integers. Run Ruff formatting and linting for Python changes.
- Make focused commits on main; do not open pull requests unless requested.
