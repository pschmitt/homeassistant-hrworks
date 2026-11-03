// Shared portal helpers for the HR WORKS run scripts. Python puts this file in front of an
// operation (login.js, snapshot.js, ...) and passes `H` (the helpers) as its second argument.
// Errors the integration understands are thrown as
// `hrworks:<code>`; everything else (Playwright timeouts, ...) counts as an unavailable browser.

const LOGIN_URL = 'https://ssl6.hrworks.de/o/dashboard';
const VIEWPORT = { width: 1600, height: 1000 };
const HRWORKS_COOKIES = /(^|\.)hrworks\.de$/;
const TIME_RE = /^([+−-]?)(\d+):(\d{2})(?:\s*(?:Hours|hours|Stunden))?$/;
const DATE_RE = /\b(\d{2})\.(\d{2})\.(\d{4}|\d{2})\b/g;
const PAIR_RE = /^(.+?)\s*\((.+)\)$/;
const ACCOUNT_LABELS = {
  'target hours': 'monthly_target',
  'sollstunden': 'monthly_target',
  'working hours': 'monthly_worked',
  'arbeitsstunden': 'monthly_worked',
  'time credit': 'time_credit',
  'zeitgutschrift': 'time_credit',
  'end of month balance': 'monthly_balance',
  'monatssaldo': 'monthly_balance',
  'transferred from previous month': 'carryover',
  'übertrag aus vormonat': 'carryover',
  'absences': 'absence_deduction',
  'abwesenheiten': 'absence_deduction',
  'total balance': 'total_balance',
  'gesamtsaldo': 'total_balance',
};
const PREVIOUS_DAY_LABELS = {
  'monthly balance to the previous day': 'monthly_balance',
  'total balance to the previous day': 'total_balance',
};
const BALANCES = new Set(['monthly_balance', 'total_balance']);
const MONTHS = {
  january: 1, february: 2, march: 3, april: 4, may: 5, june: 6, july: 7, august: 8,
  september: 9, october: 10, november: 11, december: 12,
  januar: 1, februar: 2, 'märz': 3, mai: 5, juni: 6, juli: 7, oktober: 10, dezember: 12,
};

class PortalError extends Error {
  constructor(code) {
    super(`hrworks:${code}`);
    this.code = code;
  }
}

const isTimeout = err => err && err.name === 'TimeoutError';
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const pad = n => String(n).padStart(2, '0');

function minutes(value) {
  const match = TIME_RE.exec(value.split(/\s+/).filter(Boolean).join(' '));
  if (!match || Number(match[3]) >= 60) return null;
  return (match[1] === '-' || match[1] === '−' ? -1 : 1) * (Number(match[2]) * 60 + Number(match[3]));
}

/** ISO dates (YYYY-MM-DD) of every dd.mm.yy(yy) in the text. */
function dates(value) {
  return [...value.matchAll(DATE_RE)].map(([, d, m, y]) => `${y.length === 2 ? 2000 + Number(y) : y}-${m}-${d}`);
}

function addDays(iso, days) {
  const [y, m, d] = iso.split('-').map(Number);
  return new Date(Date.UTC(y, m - 1, d + days)).toISOString().slice(0, 10);
}

/** 2026-03-12 as the portal writes it in day headers: 12.03.26 */
function portalDate(iso) {
  const [y, m, d] = iso.split('-');
  return `${d}.${m}.${y.slice(2)}`;
}

function normalize(value) {
  return value.replace(/’/g, "'").split(/\s+/).filter(Boolean).join(' ').toLowerCase().replace(/:+$/, '').trim();
}

/** Map time-account rows; values the portal does not show stay absent. */
function accountMetrics(rows) {
  const current = {};
  const previous = {};
  for (const [label, value] of rows) {
    const labels = PAIR_RE.exec(label.trim());
    const values = PAIR_RE.exec(value.trim());
    const pairs = labels && values ? [[labels[1], values[1]], [labels[2], values[2]]] : [[label, value]];
    for (const [name, text] of pairs) {
      const parsed = minutes(text);
      if (parsed === null) continue;
      const key = normalize(name);
      if (Object.hasOwn(PREVIOUS_DAY_LABELS, key)) previous[PREVIOUS_DAY_LABELS[key]] = parsed;
      else if (Object.hasOwn(ACCOUNT_LABELS, key)) current[ACCOUNT_LABELS[key]] = parsed;
    }
  }
  if (!Object.keys(previous).length) return [current, 'end_of_month'];
  const kept = Object.fromEntries(Object.entries(current).filter(([k]) => !BALANCES.has(k)));
  return [{ ...kept, ...previous }, 'previous_day'];
}

/** Make the page really render at VIEWPORT, or the portal switches to its mobile layout. */
async function ensureViewport(page) {
  await page.setViewportSize(VIEWPORT).catch(() => {});
  page.setDefaultTimeout(20000);
}

/** Wait for the portal's event queue and lazy render to become stable. */
async function settle(page) {
  let previous = null;
  let stable = 0;
  for (let i = 0; i < 40; i++) {
    await sleep(250);
    const state = await page.evaluate(() => ({
      text: document.body.innerText,
      busy: (window.jQuery?.active || 0) > 0,
      shimmer: [...document.querySelectorAll('.hrw-loadingShimmer')]
        .some(e => e.checkVisibility() && !e.closest('.me-data-list-group')),
    })).catch(err => {
      // The portal still redirects (e.g. the login page to the dashboard): not settled yet.
      if (/Execution context was destroyed|navigat/i.test(err.message)) return null;
      throw err;
    });
    if (!state) {
      stable = 0;
      previous = null;
      continue;
    }
    stable = state.text === previous && !state.busy ? stable + 1 : 0;
    previous = state.text;
    if (stable >= 4 && !state.shimmer) return;
  }
  throw new PortalError('portal_changed');
}

const onLoginPage = page => page.url().includes('login.hrworks.de');

async function navigate(page, route, reuse = false) {
  const target = `/o/time-management/${route}`;
  const link = page.locator(`a[href$="${target}"]:visible`).first();
  const inPortal = page.url().includes('/o/');
  if (reuse && inPortal && new URL(page.url()).pathname.endsWith(target)) {
    // already there
  } else if (inPortal && await link.count()) {
    await link.click();
  } else {
    // Third-party scripts can delay DOMContentLoaded on this portal.
    await page.goto(new URL(target, inPortal ? page.url() : LOGIN_URL).href, { waitUntil: 'commit', timeout: 45000 });
    await page.locator('body').waitFor({ timeout: 45000 });
  }
  if (onLoginPage(page)) throw new PortalError('session_expired');
  try {
    await page.locator('.m-portlet').first().waitFor({ state: 'visible', timeout: 45000 });
  } catch (err) {
    if (onLoginPage(page)) throw new PortalError('session_expired');
    throw err;
  }
  await settle(page);
  if (onLoginPage(page)) throw new PortalError('session_expired');
  return page;
}

/** Where a portal address really ends up: the portal redirects to its login page only
 *  after loading, so the address right after `goto` says nothing yet. */
async function landed(page) {
  const portal = page.locator('.m-portlet:visible').first();
  await Promise.race([
    page.waitForURL(url => url.hostname.startsWith('login.'), { timeout: 30000 }).catch(() => {}),
    portal.waitFor({ state: 'visible', timeout: 30000 }).catch(() => {}),
  ]);
  if (onLoginPage(page)) return 'login';
  return page.url().includes('/o/') && await portal.count() ? 'portal' : 'other';
}

async function forgetLogin(context) {
  await context.clearCookies({ domain: HRWORKS_COOKIES });
}

async function login(page, context, { company, username, password, force }) {
  if (force) await forgetLogin(context);
  await page.goto(LOGIN_URL, { waitUntil: 'domcontentloaded' });
  if (await landed(page) === 'portal') {
    await settle(page);
    return { authenticated: true };
  }
  if (!force) {
    // An expired session may retain cookies which interfere with a new login.
    await forgetLogin(context);
    await page.goto(LOGIN_URL, { waitUntil: 'domcontentloaded' });
    await landed(page);
  }
  await page.locator('input[name=company]').fill(company);
  await page.locator('input[name=company]').press('Tab');
  await settle(page);
  await page.locator('input[name=login]').fill(username);
  await page.locator('input[type=password]').fill(password);
  // A lasting login: later runs (and a recovery after an expired session) skip the form.
  const keep = page.getByRole('checkbox', { name: /Keep me logged in|Angemeldet bleiben/ });
  if (await keep.count()) await keep.first().check().catch(() => {});
  await page.getByRole('button', { name: /Log in to HR WORKS|Anmelden/ }).click();
  try {
    await page.waitForURL(url => url.pathname !== '/', { timeout: 15000 });
  } catch (err) {
    if (isTimeout(err)) throw new PortalError('login_timeout');
    throw err;
  }
  if (page.url().includes('2fa-selection')) {
    const authenticator = page.getByText('Authenticator', { exact: true });
    if (!await authenticator.count()) throw new PortalError('unsupported_mfa');
    await authenticator.click();
    await page.waitForURL('**/2fa-totp');
  }
  if (page.url().includes('2fa-totp')) return { mfa_required: true };
  if (!page.url().includes('/o/')) throw new PortalError('unsupported_mfa');
  await settle(page);
  return { authenticated: true };
}

async function mfa(page, code) {
  if (!/^[0-9]{6}$/.test(code)) throw new PortalError('invalid_code');
  if (!page.url().includes('2fa-totp')) throw new PortalError('session_expired');
  await page.locator('input:visible').fill(code);
  await page.getByRole('button', { name: /^Unlock$|^Freischalten$/ }).click();
  try {
    await page.waitForURL('**/o/**', { timeout: 10000, waitUntil: 'domcontentloaded' });
  } catch (err) {
    if (isTimeout(err)) throw new PortalError('invalid_code');
    throw err;
  }
  await settle(page);
  return { authenticated: true };
}

async function selectYear(page, year) {
  const selects = page.locator('select:visible');
  for (let index = 0; index < await selects.count(); index++) {
    const select = selects.nth(index);
    const labels = (await select.locator('option').allTextContents()).map(x => x.trim());
    if (labels.some(x => /^20\d{2}$/.test(x))) {
      if (!labels.includes(String(year))) return false;
      const selected = await select.locator('option:checked').innerText();
      if (selected.trim() !== String(year)) {
        await select.selectOption({ label: String(year) });
        await settle(page);
      }
      return true;
    }
  }
  throw new PortalError('portal_changed');
}

/** Trigger all waypoint pages; fail rather than publish a partial calendar. */
async function drainList(page) {
  const items = () => page.locator('.me-master-detail-list-group-item').count();
  for (let i = 0; i < 80; i++) {
    const waypoints = page.locator('.me-waypoint:visible');
    if (!await waypoints.count()) return;
    const count = await items();
    await waypoints.last().scrollIntoViewIfNeeded();
    await page.locator('.me-scroller').evaluateAll(
      es => es.forEach(e => { e.scrollTop = e.scrollHeight; e.dispatchEvent(new Event('scroll')); }));
    await settle(page);
    if (count === await items() && await page.locator('.me-waypoint:visible').count()) {
      // Waypoints need an actual crossing, not just a scroll event.
      await page.locator('.me-scroller').evaluateAll(es => es.forEach(e => { e.scrollTop = 0; }));
      await sleep(300);
      await waypoints.last().scrollIntoViewIfNeeded();
      await settle(page);
      if (await items() === count) throw new PortalError('portal_changed');
    }
  }
  throw new PortalError('portal_changed');
}

/** The newest month's time account: {metrics, period, balance_basis}. */
async function account(page, year) {
  await navigate(page, 'working-time-months');
  const none = { metrics: {}, period: null, balance_basis: null };
  if (!await selectYear(page, year)) return none;
  // First item is the newest available month; do not rely on old selected preferences.
  const items = page.locator('.me-master-detail-list-group-item').filter({ hasNotText: /Not worked|Nicht gearbeitet/i });
  if (!await items.count()) return none;
  const title = await items.first().locator('.m-widget4__title').innerText();
  const match = /^\s*(\p{L}+)\s+(\d{4})\s*$/u.exec(title);
  const month = match && Object.hasOwn(MONTHS, match[1].toLowerCase()) && MONTHS[match[1].toLowerCase()];
  if (!month) throw new PortalError('portal_changed');
  const period = `${match[2]}-${pad(month)}`;
  await items.first().locator('.m-widget4__title a').click();
  await settle(page);
  const rows = await page.locator('div.row').evaluateAll(es => es
    .filter(e => e.checkVisibility() && e.children.length === 2)
    .map(e => [...e.children].map(c => c.innerText.trim()))
    .filter(a => a[0].length < 90 && /^[+−-]?\d+:\d{2}(\s|$)/.test(a[1])));
  const [metrics, balanceBasis] = accountMetrics(rows);
  if (!Object.keys(metrics).length) throw new PortalError('portal_changed');
  return { metrics, period, balance_basis: balanceBasis };
}

/** Leave or sickness of a year: {events, vacation}. */
async function eventList(page, kind, year) {
  await navigate(page, kind === 'leave' ? 'absences' : 'sicknesses');
  if (!await selectYear(page, year)) return { events: [], vacation: {} };
  // Include all statuses in the source; the integration filters them explicitly.
  for (const select of await page.locator('select:visible').all()) {
    const options = await select.locator('option').allTextContents();
    const allLabel = options.find(x => ['All', 'Alle'].includes(x.trim()));
    if (allLabel && !options.some(x => /^20\d{2}$/.test(x.trim()))) {
      if ((await select.locator('option:checked').innerText()).trim() !== allLabel.trim()) {
        await select.selectOption({ label: allLabel });
        await settle(page);
      }
    }
  }
  await drainList(page);
  const items = await page.locator('.me-master-detail-list-group-item').evaluateAll(es => es
    .filter(e => e.checkVisibility()).map(e => ({
      href: e.querySelector('.m-widget4__title a')?.getAttribute('href'),
      title: e.querySelector('.m-widget4__title')?.innerText,
      subtitle: e.querySelector('.m-widget4__sub')?.innerText,
      status: e.querySelector('.me-badge')?.innerText,
      approved: !!e.querySelector('.m-badge--success'),
      count: e.querySelector('.m-widget4__ext')?.innerText,
    })));
  const expected = kind === 'leave' ? '/absence/' : '/sickness/';
  const events = [];
  for (const item of items) {
    if (!(item.href || '').includes(expected)) continue;
    const ds = dates(item.subtitle || '');
    if (!ds.length) throw new PortalError('portal_changed');
    const count = normalize(item.count || '');
    // Sickness is reported, not approved: its badge reads e.g. "eAU is available" until
    // HR checks it. Only a rejection or cancellation voids it.
    const approved = item.approved || (kind === 'sickness'
      && !/reject|declin|cancel|withdrawn|abgelehnt|storniert|zurückgezogen/.test(normalize(item.status || '')));
    const halfDay = /(?:0[.,]5|½)\s*(?:vacation|urlaub|day|tag)/.test(count);
    events.push({
      uid: `hrworks-${kind}-${item.href.replace(/\/+$/, '').split('/').pop()}`,
      kind,
      summary: item.title.split(/\s+/).filter(Boolean).join(' '),
      start: ds[0],
      // Portal ranges are inclusive. HA all-day calendar end dates are exclusive.
      end: addDays(ds[ds.length - 1], 1),
      approved,
      status: item.status || '',
      half_day: halfDay,
      description: (item.status || '') + (halfDay ? ' · Half day' : ''),
    });
  }
  const vacation = {};
  if (kind === 'leave') {
    const texts = await page.locator('.m-portlet').evaluateAll(es => es.filter(e => e.checkVisibility()).map(e => e.innerText));
    const text = texts.find(t => t.startsWith('Vacation account') || t.startsWith('Urlaubskonto')) || '';
    for (const [key, suffix] of [['vacation_remaining', 'Available|Verfügbar'], ['vacation_approved', 'Approved|Genehmigt']]) {
      const match = new RegExp(`(\\d+(?:[.,]\\d+)?)\\s+(?:${suffix})`).exec(text);
      if (match) vacation[key] = parseFloat(match[1].replace(',', '.'));
    }
  }
  return { events, vacation };
}

/** Read the visible editor rows without changing the portal editor. */
async function editorEntries(holder) {
  const fields = await holder.locator('label').evaluateAll(labels => labels
    .filter(label => label.checkVisibility() || label.closest('.me-form-group, .form-group')?.checkVisibility())
    .map(label => {
      const group = label.closest('.me-form-group, .form-group');
      const control = label.control || label.querySelector('input,select,textarea')
        || group?.querySelector('input,select,textarea');
      return {
        label: label.innerText,
        value: control?.selectedOptions?.[0]?.text ?? control?.value
          ?? group?.querySelector('.me-form-item-div')?.innerText,
      };
    }));
  const entries = [];
  let current = null;
  for (const f of fields) {
    const label = normalize(f.label || '').replace(/:+$/, '');
    const value = (f.value || '').trim();
    if (['start time', 'beginn', 'startzeit'].includes(label) && value) {
      if (current) entries.push(current);
      current = { start: value };
    } else if (current && ['end time', 'ende', 'endzeit'].includes(label)) current.end = value;
    else if (current && ['working time type', 'arbeitszeitart'].includes(label)) current.type = value;
    else if (current && ['comment', 'kommentar', 'bemerkung'].includes(label)) current.comment = value;
  }
  if (current) entries.push(current);
  if (entries.some(e => !('end' in e) || !e.type)) throw new PortalError('portal_changed');
  if (!entries.length && await holder.locator('input:visible,select:visible,textarea:visible').count()) {
    throw new PortalError('portal_changed');
  }
  return entries;
}

/** The header row of a day on the working-times page, opened for reading. `row` is null
 *  when the listed week has no row for the day, which is a day without target or time. */
async function dayHeader(page, day, { reuse = false } = {}) {
  await navigate(page, 'working-times', reuse);
  const [year, month] = day.split('-').map(Number);
  // The portal lists only recent years; anything else is not a day it can show.
  if (!await selectYear(page, year)) return { offered: false, row: null };
  for (const select of await page.locator('select:visible').all()) {
    const labels = await select.locator('option').allTextContents();
    if (labels.length === 12) {
      if (await select.evaluate(e => e.selectedIndex) !== month - 1) {
        await select.selectOption({ index: month - 1 });
        await settle(page);
      }
      break;
    }
  }
  let offered = false;
  for (const tab of await page.locator('.m-accordion__item-head').all()) {
    const title = await tab.locator('.m-accordion__item-title').innerText();
    const span = /(\d{2})\.(\d{2})\.\s*-\s*(\d{2})\.(\d{2})\./.exec(title);
    if (span) {
      const from = `${year}-${span[2]}-${span[1]}`;
      const to = `${year}-${span[4]}-${span[3]}`;
      if (from <= day && day <= to) {
        offered = true;
        if ((await tab.getAttribute('class') || '').split(/\s+/).includes('collapsed')) {
          await tab.click();
          await settle(page);
        }
        break;
      }
    }
  }
  // A month without the week of that day is a changed portal, not a day without time.
  if (!offered) throw new PortalError('portal_changed');
  const header = page.locator('.hrw-me-working-time-day-header-row').filter({ hasText: portalDate(day) });
  // An open week lists only days with a target or recorded time.
  if (!await header.count()) return { offered, row: null };
  if (!await header.isVisible()) throw new PortalError('portal_changed');
  return { offered, row: header };
}

/** Actual day entries and metrics; every split interval stays separate. */
async function workingDay(page, day, { reuse = false } = {}) {
  const { offered, row: header } = await dayHeader(page, day, { reuse });
  if (!offered) return { metrics: {}, entries: [] };
  if (header === null) {
    // A day without a row in its listed week has no target, no time and no balance.
    return { metrics: { today_target: 0, today_worked: 0, today_balance: 0 }, entries: [] };
  }
  const holder = header.locator('..').first();
  // Expansion is retained by the portal. Never toggle an already-open editor closed.
  if (!await holder.locator('input:visible, select:visible').count()) {
    await header.locator(':scope > div').first().click();
    await settle(page);
  }
  const entries = await editorEntries(holder);
  const headerText = await header.innerText();
  if (!entries.length && /Open working time|Offene Arbeitszeit|\b\d{2}:\d{2}\s*\n\s*\d{2}:\d{2}\b/i.test(headerText)) {
    throw new PortalError('portal_changed');
  }
  // The info popup reports net credited work and target time, including break rules.
  const info = header.locator('a').filter({ has: page.locator('.icon-streamline-information-circle') });
  const metrics = {};
  for (const text of await header.locator('span.hrw-me-working-time-day-header-span').allTextContents()) {
    const parsed = minutes(text.trim());
    if (parsed !== null) metrics.today_balance = parsed;
  }
  if (await info.count()) {
    await info.click();
    await settle(page);
    const rows = await page.locator('div.row').evaluateAll(es => es
      .filter(e => e.checkVisibility() && e.children.length === 2)
      .map(e => [...e.children].map(c => c.innerText.trim()))
      .filter(a => a[0].length < 80 && /^[+-]?\d+:\d{2}/.test(a[1])));
    const keys = {
      'target working time': 'today_target',
      'target hours': 'today_target',
      'actual working time': 'today_worked',
      'working time': 'today_worked',
      'balance': 'today_balance',
    };
    for (const [label, value] of rows) {
      const key = Object.hasOwn(keys, normalize(label)) ? keys[normalize(label)] : undefined;
      const val = minutes(value);
      if (key && val !== null) metrics[key] = val;
    }
    const tooltip = await holder.innerText();
    for (const [key, label] of [
      ['today_target', 'Target hours|Sollstunden'],
      ['today_worked', 'Credited working hours|Angerechnete Arbeitsstunden'],
    ]) {
      const match = new RegExp(`(?:${label}):\\s*([+-]?\\d+:\\d{2})`).exec(tooltip);
      if (match) metrics[key] = minutes(match[1]);
    }
    await info.click();
    await settle(page);
  }
  return { metrics, entries };
}

const rowOf = field => field.locator("xpath=ancestor::div[contains(concat(' ',normalize-space(@class),' '),' row ')][2]");
const byId = (page, id) => page.locator(`[id=${JSON.stringify(id)}]`);

/** Remove a request's own draft row; never guess which entry to delete. */
async function discardRow(page, startField) {
  const row = rowOf(startField);
  await row.locator('a.m-dropdown__toggle').click();
  await settle(page);
  const del = row.getByRole('link', { name: /^(Delete|Löschen)$/ });
  if (await del.count() !== 1) throw new PortalError('write_uncertain');
  // Portal dropdowns may extend outside the scroll container.
  await del.evaluate(el => el.click());
  await settle(page);
  const modal = page.locator('.modal.show');
  if (await modal.count() !== 1) throw new PortalError('write_uncertain');
  await modal.getByRole('button', { name: /^(Yes|Ja)$/ }).click();
  await settle(page);
  if (await startField.count()) throw new PortalError('write_uncertain');
}

const H = {
  PortalError, LOGIN_URL, ensureViewport, settle, navigate, login, mfa, account, eventList,
  workingDay, dayHeader, editorEntries, discardRow, rowOf, byId, minutes, normalize, addDays,
  portalDate, sleep,
};
