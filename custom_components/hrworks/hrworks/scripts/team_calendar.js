// Team and organizational unit absence calendar query
async ({ page, args }, H) => {
  const { PortalError, ensureViewport, sleep } = H;
  await ensureViewport(page);

  const orgUnit = args.organizational_unit || args.ou || null;

  // 1. Navigate to target calendar view
  let targetUrl = 'https://ssl6.hrworks.de/o/calendar-this-week';
  if (orgUnit) {
    targetUrl = 'https://ssl6.hrworks.de/o/company-calendar/calendar-organizational-unit';
  }

  const inPortal = page.url().includes('/o/');
  if (!inPortal || !page.url().includes(targetUrl)) {
    await page.goto(targetUrl, { waitUntil: 'commit', timeout: 45000 });
    await page.locator('body').waitFor({ timeout: 45000 });
  }

  if (page.url().includes('login.hrworks.de')) throw new PortalError('session_expired');

  // Wait for calendar to be visible
  const cal = page.locator('.me-full-calendar').first();
  await cal.waitFor({ state: 'visible', timeout: 30000 });

  // 2. If organizational unit is requested, select it in dropdown
  if (orgUnit) {
    const select = page.locator('select').first();
    if (await select.count()) {
      const options = await select.locator('option').allTextContents();
      const normQuery = orgUnit.toLowerCase().trim();
      let matchIdx = -1;

      // Find exact or partial match
      for (let i = 0; i < options.length; i++) {
        const text = options[i].toLowerCase().trim();
        if (text === normQuery || text.includes(normQuery)) {
          matchIdx = i;
          break;
        }
      }

      if (matchIdx !== -1) {
        await page.evaluate((idx) => {
          const s = document.querySelector('select');
          if (s) {
            s.selectedIndex = idx;
            window.jQuery?.(s).trigger('change');
          }
        }, matchIdx);
        await sleep(2500);
      }
    }

    // Ensure "Include subordinate organizational units" is checked
    const cb = page.locator('input[type="checkbox"]');
    if ((await cb.count()) && !(await cb.isChecked())) {
      await page.evaluate(() => {
        const c = document.querySelector('input[type="checkbox"]');
        if (c && !c.checked) {
          c.checked = true;
          window.jQuery?.(c).trigger('change');
        }
      });
      await sleep(2500);
    }
  }

  // Wait up to 20s for cachedEvents and resources to populate
  for (let i = 0; i < 40; i++) {
    const ready = await page.evaluate(() => {
      const keys = Object.keys(window).filter((k) => k.includes('cachedEvents'));
      if (!keys.length) return false;
      const cached = window[keys[0]];
      if (!cached) return false;
      if (cached.clwLoading || cached.clwLoadingResources) return false;
      const hasDomResources = document.querySelectorAll('.me-full-calendar .fc-resource-area tr[data-resource-id]').length > 0;
      const hasCachedResources = Array.isArray(cached.resources) && cached.resources.length > 0;
      return hasDomResources || hasCachedResources;
    }).catch(() => false);
    if (ready) {
      await sleep(1000);
      break;
    }
    await sleep(500);
  }

  // 3. Extract cached events and resources from page context
  const data = await page.evaluate(() => {
    const keys = Object.keys(window).filter((k) => k.includes('cachedEvents'));
    if (!keys.length) return { resources: [], absences: [] };
    const cached = window[keys[0]];
    const calEl = document.querySelector('.me-full-calendar');

    // Extract resources from DOM tr elements or cached resources
    const resourceMap = {};
    if (calEl) {
      const rows = calEl.querySelectorAll('.fc-resource-area tr[data-resource-id]');
      for (const row of rows) {
        const id = row.getAttribute('data-resource-id');
        const text = row.querySelector('.fc-cell-text')?.innerText?.trim() || row.innerText?.trim();
        if (id && text && text !== 'Loading') {
          let displayName = text;
          let firstName = '';
          let lastName = text;
          if (text.includes(',')) {
            const parts = text.split(',').map((p) => p.trim());
            lastName = parts[0];
            firstName = parts.slice(1).join(' ');
            displayName = `${firstName} ${lastName}`;
          }
          resourceMap[id] = {
            id,
            rawName: text,
            displayName,
            firstName,
            lastName,
          };
        }
      }
    }

    // Fallback to cached.resources if DOM was incomplete
    if (Array.isArray(cached?.resources)) {
      for (const r of cached.resources) {
        const id = window.jQuery?.(r).data('resourceId') || r.getAttribute?.('data-resource-id');
        const text = r.innerText?.trim();
        if (id && text && !resourceMap[id] && text !== 'Loading') {
          let displayName = text;
          let firstName = '';
          let lastName = text;
          if (text.includes(',')) {
            const parts = text.split(',').map((p) => p.trim());
            lastName = parts[0];
            firstName = parts.slice(1).join(' ');
            displayName = `${firstName} ${lastName}`;
          }
          resourceMap[id] = { id, rawName: text, displayName, firstName, lastName };
        }
      }
    }

    // Extract events
    const rawEvents = cached?.cachedCalendarEvents || [];
    const absences = [];

    for (const ev of rawEvents) {
      if (!ev.id || !ev.id.startsWith('calendarEvent_')) continue;
      const resIds = ev.resourceIds || [];
      const category = ev.extendedProps?.legendCategory || 'Absent';
      const tooltip = ev.extendedProps?.tooltipText || '';

      let status = '';
      if (tooltip.includes('Status:')) {
        const match = tooltip.match(/Status:<\/b>\s*([^<]+)/i);
        if (match) status = match[1].trim();
      }

      for (const resId of resIds) {
        const emp = resourceMap[resId] || { id: resId, displayName: resId, rawName: resId };
        absences.push({
          employee: emp.displayName,
          rawName: emp.rawName,
          resourceId: resId,
          category,
          status,
          start: ev.start,
          end: ev.end,
          tooltip,
        });
      }
    }

    return {
      employees: Object.values(resourceMap),
      absences,
    };
  });

  return {
    unit: orgUnit || 'team',
    employees: data.employees,
    absences: data.absences,
  };
};
