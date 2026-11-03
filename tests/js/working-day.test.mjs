// Day reads of day.js and record.js against a synthetic working-times page.
import assert from 'node:assert/strict';
import { after, before, describe, test } from 'node:test';
import { errorCode, html, open } from './harness.mjs';

/** A working-times page with one open week. Like the portal, the week lists only days
 *  with a target or recorded time: `rows` maps a day header (e.g. 'Fr, 23.10.26') to its
 *  balance. */
function workingTimes({ month, week, rows }) {
  const months = Array.from({ length: 12 }, (_, index) => (
    `<option${index === month - 1 ? ' selected' : ''}>${index + 1}</option>`)).join('');
  const days = Object.entries(rows).map(([day, balance]) => `<div>
    <div class="hrw-me-working-time-day-header-row"><div>${day}</div>
    <span class="hrw-me-working-time-day-header-span">${balance} hours</span></div></div>`);
  return html(`<div class="m-portlet">
    <select><option>2025</option><option selected>2026</option></select>
    <select>${months}</select>
    <div class="m-accordion__item-head"><span class="m-accordion__item-title">
      ${week}</span></div>
    ${days.join('')}
  </div>`);
}

const NO_TARGET = { metrics: { today_target: 0, today_worked: 0, today_balance: 0 }, entries: [] };

describe('working day', () => {
  let portal;

  before(async () => {
    const page = workingTimes({
      month: 10,
      week: 'CW 43 (19.10. - 25.10.)',
      rows: { 'Fr, 23.10.26': '-04:00', 'Mo, 19.10.26': '+00:00' },
    });
    portal = await open(() => page);
  });

  after(() => portal.close());

  test('a listed day is read', async () => {
    assert.deepEqual(
      await portal.run('day', { date: '2026-10-23' }),
      { metrics: { today_balance: -240 }, entries: [] },
    );
  });

  test('a free weekday or a Sunday without a row has no target and no time', async () => {
    for (const date of ['2026-10-22', '2026-10-25']) {
      assert.deepEqual(await portal.run('day', { date }), NO_TARGET, date);
    }
  });

  test('a year the portal does not offer has no metrics', async () => {
    assert.deepEqual(await portal.run('day', { date: '2024-10-22' }), { metrics: {}, entries: [] });
  });

  test('a day outside the listed weeks fails as portal_changed', async () => {
    assert.equal(await errorCode(portal.run('day', { date: '2026-10-12' })), 'portal_changed');
  });

  test('a save on a day without a row is refused before any write', async () => {
    const args = {
      date: '2026-10-22', start: '08:00', end: '12:00', type: 'work', comment: '',
      labels: ['Working time'], dry_run: false,
    };
    assert.equal(await errorCode(portal.run('record', args)), 'invalid_interval');
  });
});

describe('a week over New Year', () => {
  // The portal shows the same week in the December and the January view.
  for (const [month, date, expected] of [
    [1, '2026-01-02', { metrics: { today_balance: 60 }, entries: [] }],
    [1, '2026-01-01', NO_TARGET],
    [12, '2025-12-29', { metrics: { today_balance: -30 }, entries: [] }],
    [12, '2025-12-31', NO_TARGET],
  ]) {
    test(`${date} is found in the ${month === 1 ? 'January' : 'December'} view`, async () => {
      const page = workingTimes({
        month,
        week: 'CW 1 (29.12. - 04.01.)',
        rows: { 'Mo, 29.12.25': '-00:30', 'Fr, 02.01.26': '+01:00' },
      });
      const portal = await open(() => page);
      try {
        assert.deepEqual(await portal.run('day', { date }), expected);
      } finally {
        await portal.close();
      }
    });
  }
});
