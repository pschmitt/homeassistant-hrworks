// Login outcomes of login.js and mfa.js against a synthetic portal.
import assert from 'node:assert/strict';
import { afterEach, beforeEach, describe, test } from 'node:test';
import { errorCode, html, open } from './harness.mjs';

const CREDENTIALS = {
  company: 'synthetic-co', username: 'synthetic-user', password: 'synthetic-pw', force: true,
};
const DASHBOARD = 'https://ssl6.hrworks.de/o/dashboard';
const FORM = action => html(`<input name="company"><input name="login"><input type="password">
  <button onclick="${action}">Log in to HR WORKS</button><p id="message"></p>`);

describe('login', () => {
  let portal;
  let state;

  beforeEach(async () => {
    // `submit` is the portal's answer to the login form: a path to go to, or an action.
    state = { authenticated: false, submit: '/2fa-totp', factors: ['Authenticator'] };
    portal = await open(url => {
      if (url.hostname === 'ssl6.hrworks.de') {
        // Like the portal: a signed-out dashboard sends the browser to the login host.
        return state.authenticated
          ? html('<div class="m-portlet">Synthetic dashboard</div>')
          : html("<script>location.replace('https://login.hrworks.de/')</script>");
      }
      switch (url.pathname) {
        case '/':
          if (state.submit === 'rejected') {
            return FORM("document.getElementById('message').textContent='Login failed'");
          }
          if (state.submit === 'stuck') return FORM("document.body.innerHTML='<p>Please wait</p>'");
          if (state.submit === 'slow') return FORM("fetch('/authenticate', { method: 'POST' })");
          if (state.submit === 'late') {
            return FORM("setTimeout(() => { location.href = '/2fa-totp'; }, 1800)");
          }
          return FORM(`location.href='${state.submit}'`);
        case '/2fa-selection':
          return html(state.factors.map(name => (name === 'Authenticator' && !state.stuckSelection
            ? `<button onclick="location.href='/2fa-totp'">${name}</button>`
            : `<button>${name}</button>`)).join(''));
        case '/2fa-totp':
          state.authenticated = true;
          return html(`<input><button onclick="if (document.querySelector('input').value
            === '123456') location.href='${DASHBOARD}'">Unlock</button>`);
        case '/2fa-setup':
          return html('<p>Set up a second factor</p>');
        case '/authenticate':
          return null;
        default:
          return html('<p>Please wait</p>');
      }
    });
  });

  afterEach(() => portal.close());

  const login = () => portal.run('login', CREDENTIALS);

  test('a TOTP login asks for the code and completes with it', async () => {
    assert.deepEqual(await login(), { mfa_required: true });
    assert.deepEqual(await portal.run('mfa', { code: '123456' }), { authenticated: true });
  });

  test('the factor selection chooses Authenticator', async () => {
    state.submit = '/2fa-selection';
    state.factors = ['E-mail', 'Authenticator'];
    assert.deepEqual(await login(), { mfa_required: true });
  });

  test('a factor selection without Authenticator is unsupported_mfa', async () => {
    state.submit = '/2fa-selection';
    state.factors = ['E-mail'];
    assert.equal(await errorCode(login()), 'unsupported_mfa');
  });

  test('another second-factor step is unsupported_mfa', async () => {
    state.submit = '/2fa-setup';
    assert.equal(await errorCode(login()), 'unsupported_mfa');
  });

  test('rejected credentials are invalid_auth', async () => {
    state.submit = 'rejected';
    assert.equal(await errorCode(login()), 'invalid_auth');
  });

  test('a login the server has not answered yet is login_timeout, not invalid_auth', async () => {
    state.submit = 'slow';
    assert.equal(await errorCode(login()), 'login_timeout');
  });

  test('a login that moves on just after the step timeout still completes', async () => {
    state.submit = 'late';
    assert.deepEqual(await login(), { mfa_required: true });
  });

  test('a login form that goes nowhere is login_timeout, not invalid_auth', async () => {
    state.submit = 'stuck';
    assert.equal(await errorCode(login()), 'login_timeout');
  });

  test('an unknown page after the login is login_timeout', async () => {
    state.submit = '/pending';
    assert.equal(await errorCode(login()), 'login_timeout');
  });

  test('Authenticator that never shows the code step is login_timeout', async () => {
    state.submit = '/2fa-selection';
    state.stuckSelection = true;
    assert.equal(await errorCode(login()), 'login_timeout');
  });
});
