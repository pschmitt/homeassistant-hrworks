// Run the browser-daemon scripts (custom_components/hrworks/hrworks/scripts) against a
// synthetic portal. Every request of the context is answered by the test: no network,
// no real portal and no employee data.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const SCRIPTS = new URL('../../custom_components/hrworks/hrworks/scripts/', import.meta.url);
// A short login step keeps the timeout cases fast; the logic under test stays unchanged.
const LOGIN_STEP = [/const LOGIN_STEP_TIMEOUT = \d+;/, 'const LOGIN_STEP_TIMEOUT = 1500;'];

const read = name => readFileSync(fileURLToPath(new URL(`${name}.js`, SCRIPTS)), 'utf8').trim();

/** The run script of an operation, assembled like `browser_daemon.script()` does. */
export function script(operation) {
  const helpers = read('helpers').replace(...LOGIN_STEP);
  const source = `async (ctx) => {\n${helpers}\nconst operation = ${read(operation)};\n`
    + 'return await operation(ctx, H);\n}';
  return new Function(`return ${source};`)();
}

export const html = body => `<!doctype html><html><body>${body}</body></html>`;

/** A browser whose context answers every request with `serve(url)` (an HTML string). */
export async function open(serve) {
  const browser = await chromium.launch();
  const context = await browser.newContext();
  await context.route('**/*', route => route.fulfill({
    contentType: 'text/html',
    body: serve(new URL(route.request().url())),
  }));
  const page = await context.newPage();
  return {
    page,
    context,
    run: (operation, args) => script(operation)({ page, context, args }),
    close: () => browser.close(),
  };
}

/** The `hrworks:<code>` of a rejected run, as the integration reads it. */
export async function errorCode(promise) {
  try {
    await promise;
  } catch (err) {
    const match = /^hrworks:([a-z_]+)$/.exec(err.message);
    if (match) return match[1];
    throw err;
  }
  throw new Error('the run did not fail');
}
