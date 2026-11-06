// Company-wide absences: iterate a batch of organizational units in one page.
// args.units: list of unit names (exact option labels, whitespace-insensitive).
// Without units, only the available unit list is returned (cheap discovery call).
async ({ page, args }, H) => {
  const { PortalError, ensureViewport, sleep } = H;
  await ensureViewport(page);

  const URL = 'https://ssl6.hrworks.de/o/company-calendar/calendar-organizational-unit';
  if (!page.url().includes(URL)) {
    await page.goto(URL, { waitUntil: 'commit', timeout: 45000 });
    await page.locator('body').waitFor({ timeout: 45000 });
  }
  if (page.url().includes('login.hrworks.de')) throw new PortalError('session_expired');
  await page.locator('.me-full-calendar').first().waitFor({ state: 'visible', timeout: 30000 });

  const clean = (s) => String(s || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
  const options = (await page.locator('select').first().locator('option').allTextContents()).map(clean);
  const units = options.filter((o) => o && o.toLowerCase() !== 'all');

  const requested = Array.isArray(args.units) ? args.units.map(clean).filter(Boolean) : [];
  if (!requested.length) return { available_organizational_units: units, employees: [], absences: [], units: [] };

  // Only direct members per unit: subordinate units are fetched as units of their own.
  const uncheckedSub = await page.evaluate(() => {
    const c = document.querySelector('input[type="checkbox"]');
    if (c && c.checked) {
      c.checked = false;
      window.jQuery?.(c).trigger('change');
      return true;
    }
    return false;
  });

  const waitLoaded = async (prevSig = null) => {
    await sleep(600);
    for (let i = 0; i < 40; i++) {
      const state = await page.evaluate(() => {
        const key = Object.keys(window).find((k) => k.includes('cachedEvents'));
        const cached = key && window[key];
        if (!cached || cached.clwLoading || cached.clwLoadingResources) return { ready: false };
        const calEl = document.querySelector('.me-full-calendar');
        const domRows = [...(calEl?.querySelectorAll('.fc-resource-area tr[data-resource-id]') || [])];
        const loadingRow = domRows.some((r) => (r.innerText || '').trim() === 'Loading');
        if (loadingRow) return { ready: false };
        const sig = domRows.map((r) => `${r.getAttribute('data-resource-id')}:${(r.innerText || '').trim()}`).join('|');
        return { ready: true, sig };
      }).catch(() => ({ ready: false }));
      if (state.ready && (prevSig === null || state.sig !== prevSig || i >= 4)) {
        await sleep(300);
        return state.sig;
      }
      await sleep(400);
    }
    return null;
  };

  let lastSig = uncheckedSub ? await waitLoaded() : null;

  const extract = () => page.evaluate(() => {
    const key = Object.keys(window).find((k) => k.includes('cachedEvents'));
    const cached = key && window[key];
    const calEl = document.querySelector('.me-full-calendar');
    const names = {};
    for (const row of calEl?.querySelectorAll('.fc-resource-area tr[data-resource-id]') || []) {
      const id = row.getAttribute('data-resource-id');
      const text = (row.querySelector('.fc-cell-text')?.innerText || row.innerText || '').trim();
      if (id && text && text !== 'Loading') names[id] = text;
    }
    for (const r of Array.isArray(cached?.resources) ? cached.resources : []) {
      const id = window.jQuery?.(r).data('resourceId') || r.getAttribute?.('data-resource-id');
      const text = (r.innerText || '').trim();
      if (id && text && !names[id] && text !== 'Loading') names[id] = text;
    }
    const events = (cached?.cachedCalendarEvents || []).map((ev) => ({
      resourceIds: Array.isArray(ev.resourceIds) ? ev.resourceIds : ev.resourceId ? [ev.resourceId] : [],
      category: ev.extendedProps?.legendCategory || 'Absent',
      tooltip: ev.extendedProps?.tooltipText || '',
      start: ev.start,
      end: ev.end,
    }));
    return { names, events };
  });

  const employees = {};
  const absences = [];
  const seen = new Set();
  const fetched = [];
  const failed = [];

  for (const unit of requested) {
    const idx = options.findIndex((o) => o.toLowerCase() === unit.toLowerCase());
    if (idx < 0) {
      failed.push(unit);
      continue;
    }
    const alreadySelected = await page.evaluate((i) => {
      const s = document.querySelector('select');
      if (!s) return false;
      if (s.selectedIndex === i) return true;
      s.selectedIndex = i;
      window.jQuery?.(s).trigger('change');
      return false;
    }, idx);
    const sig = await waitLoaded(alreadySelected ? null : lastSig);
    if (sig === null) {
      failed.push(unit);
      continue;
    }
    lastSig = sig;
    const { names, events } = await extract();
    // People are listed "Last, First"; rows without a comma are organizational units.
    for (const [id, raw] of Object.entries(names)) {
      if (!raw.includes(',') || employees[raw]) continue;
      const [last, ...rest] = raw.split(',').map((p) => p.trim());
      const first = rest.join(' ');
      employees[raw] = { id: raw, rawName: raw, displayName: `${first} ${last}`, firstName: first, lastName: last, unit };
    }
    for (const ev of events) {
      for (const resId of ev.resourceIds) {
        const raw = names[resId];
        if (!raw || !raw.includes(',')) continue;
        const dedupe = `${raw}|${ev.start}|${ev.end}|${ev.category}`;
        if (seen.has(dedupe)) continue;
        seen.add(dedupe);
        const m = ev.tooltip.match(/Status:<\/b>\s*([^<]+)/i);
        absences.push({
          employee: employees[raw].displayName,
          rawName: raw,
          resourceId: raw,
          category: ev.category,
          status: m ? m[1].trim() : '',
          start: ev.start,
          end: ev.end,
          unit,
        });
      }
    }
    fetched.push(unit);
  }

  return {
    available_organizational_units: units,
    units: fetched,
    failed_units: failed,
    employees: Object.values(employees),
    absences,
  };
};
