// Submit one working-time interval, preserving all existing rows and refusing ambiguity.
// Runs only for a non-dry request that the integration's write switch let through; the
// interval is validated in Python (browser_daemon.py) before it gets here. With dry_run
// nothing is typed or saved: the day is read and the request checked against it.
async ({ page, args }, H) => {
  const { PortalError, normalize, minutes, settle } = H;
  await H.ensureViewport(page);
  const { date: day, start: wantedStart, end: wantedEnd, type: kind, comment, dry_run: dryRun } = args;
  const labels = new Set(args.labels.map(normalize));
  const { entries } = await H.workingDay(page, day);
  const lower = minutes(wantedStart);
  const upper = minutes(wantedEnd);
  for (const entry of entries) {
    const entryStart = minutes(entry.start || '');
    const entryEnd = minutes(entry.end || '');
    if (entryStart === null || entryEnd === null || entryStart >= entryEnd) throw new PortalError('overlap');
    if (entryStart === lower && entryEnd === upper && labels.has(normalize(entry.type || ''))
        && (entry.comment || '') === comment) {
      return { saved: false, already_exists: true, dry_run: dryRun };
    }
    if (lower < entryEnd && upper > entryStart) throw new PortalError('overlap');
  }
  const result = {
    saved: false, dry_run: dryRun, date: day, start: wantedStart, end: wantedEnd, type: kind,
    duration_minutes: upper - lower,
  };
  if (dryRun) return result;

  const header = page.locator('.hrw-me-working-time-day-header-row').filter({ hasText: H.portalDate(day) });
  // A day without a row offers no editor: refuse before touching the portal.
  if (!await header.count()) throw new PortalError('invalid_interval');
  const add = header.locator('a').filter({ has: page.locator('.icon-streamline-add') });
  if (!await add.count() || !await add.isVisible()) throw new PortalError('writes_disabled');
  const holder = header.locator('..').first();
  const controls = holder.locator('input,select,textarea');
  const same = (a, b) => JSON.stringify(Object.entries(a).sort()) === JSON.stringify(Object.entries(b).sort());
  const ids = () => controls.evaluateAll(es => Object.fromEntries(es.map(e => [e.id, e.value])));
  let newStart = null;
  let save;
  try {
    await add.click();
    await settle(page);
    const groups = holder.locator('.me-form-group').filter({ has: page.locator('input,select,textarea') });
    const starts = groups
      .filter({ has: page.locator('label', { hasText: /^\s*(Start time|Startzeit|Beginn)\s*$/i }) })
      .locator('input');
    // Adding a row regenerates the existing fields' DOM IDs. Existing intervals have closed
    // ends (validated above); only the new row is open.
    const actual = await H.editorEntries(holder);
    if (actual.length !== entries.length + 1 || entries.some(e => !actual.some(a => same(a, e)))) {
      throw new PortalError('write_uncertain');
    }
    const added = [];
    for (const node of await starts.all()) {
      const endControl = H.rowOf(node).locator('.me-form-group')
        .filter({ has: page.locator('label', { hasText: /^\s*(End time|Endzeit|Ende)\s*$/i }) })
        .locator('input');
      if (await endControl.count() === 1 && !await endControl.inputValue()) added.push(node);
    }
    if (added.length !== 1) throw new PortalError('write_uncertain');
    const newId = await added[0].getAttribute('id');
    if (!newId) throw new PortalError('write_uncertain');
    newStart = H.byId(page, newId);
    const row = H.rowOf(newStart);
    const newIds = new Set(await row.locator('input,select,textarea').evaluateAll(es => es.map(e => e.id)));
    const previousValues = Object.entries(await ids()).filter(([key]) => !newIds.has(key));

    const field = async pattern => {
      const control = row.locator('.me-form-group')
        .filter({ has: page.locator('label', { hasText: pattern }) })
        .locator('input,select,textarea');
      if (await control.count() !== 1) throw new PortalError('portal_changed');
      const identifier = await control.getAttribute('id');
      if (!identifier) throw new PortalError('portal_changed');
      return H.byId(page, identifier);
    };
    const endField = await field(/^\s*(End time|Endzeit|Ende)\s*$/i);
    const select = await field(/^\s*(Working time type|Arbeitszeitart)\s*$/i);
    const commentField = await field(/^\s*(Comment|Kommentar|Bemerkung)\s*$/i);
    const options = await select.locator('option').allTextContents();
    const chosen = options.find(label => labels.has(normalize(label)));
    if (chosen === undefined) throw new PortalError('writes_disabled');
    for (const [control, value] of [[newStart, wantedStart], [endField, wantedEnd]]) {
      await control.press('ControlOrMeta+A');
      await control.pressSequentially(value);
      await control.press('Tab');
    }
    await select.selectOption({ label: chosen });
    await commentField.fill(comment);
    await commentField.press('Tab');
    await settle(page);
    if (await newStart.inputValue() !== wantedStart
        || await endField.inputValue() !== wantedEnd
        || await select.locator('option:checked').innerText() !== chosen
        || await commentField.inputValue() !== comment) {
      throw new PortalError('portal_changed');
    }
    const current = await ids();
    if (previousValues.some(([key, value]) => current[key] !== value)) throw new PortalError('portal_changed');
    save = page.getByRole('button', { name: /^\s*(Save|Speichern)\s*$/ });
    if (await save.count() !== 1) throw new PortalError('portal_changed');
  } catch (err) {
    // The portal retains added rows across sessions even before Save. Remove only the field
    // identified as newly added by this request.
    if (newStart === null) throw new PortalError('write_uncertain');
    try {
      await H.discardRow(page, newStart);
    } catch {
      throw new PortalError('write_uncertain');
    }
    throw err;
  }
  try {
    await save.click();
    await settle(page);
    await page.reload({ waitUntil: 'commit', timeout: 45000 });
    const { entries: verified } = await H.workingDay(page, day);
    const matches = verified.filter(e => e.start === wantedStart && e.end === wantedEnd
      && labels.has(normalize(e.type || '')) && (e.comment || '') === comment);
    const key = e => JSON.stringify([e.start, e.end, e.type, e.comment || '']);
    const known = new Set(verified.map(key));
    if (matches.length !== 1 || entries.some(e => !known.has(key(e)))) throw new PortalError('write_uncertain');
  } catch {
    throw new PortalError('write_uncertain');
  }
  return { ...result, saved: true };
}
