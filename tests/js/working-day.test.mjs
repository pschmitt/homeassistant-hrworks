// Day reads of day.js and record.js against a synthetic working-times page.
import assert from 'node:assert/strict';
import { after, before, describe, test } from 'node:test';
import { errorCode, html, open } from './harness.mjs';

const MONTHS = Array.from({ length: 12 }, (_, index) => (
  `<option${index === 9 ? ' selected' : ''}>${index + 1}</option>`)).join('');
// Like the portal, the open week lists only days with a target or recorded time.
const WORKING_TIMES = html(`<div class="m-portlet">
  <select><option>2025</option><option selected>2026</option></select>
  <select>${MONTHS}</select>
  <div class="m-accordion__item-head"><span class="m-accordion__item-title">
    CW 43 (19.10. - 25.10.)</span></div>
  <div><div class="hrw-me-working-time-day-header-row"><div>Fr, 23.10.26</div>
    <span class="hrw-me-working-time-day-header-span">-04:00 hours</span></div></div>
  <div><div class="hrw-me-working-time-day-header-row"><div>Mo, 19.10.26</div>
    <span class="hrw-me-working-time-day-header-span">+00:00 hours</span></div></div>
</div>`);
const NO_TARGET = { metrics: { today_target: 0, today_worked: 0, today_balance: 0 }, entries: [] };

describe('working day', () => {
  let portal;

  before(async () => {
    portal = await open(() => WORKING_TIMES);
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
