#!/usr/bin/env node
/**
 * OIDC identity warmup for the Playwright runner.
 *
 * Logs in every Keycloak test user once, serially, through the same browser
 * flow the suite's loginUser fixture uses. This must happen BEFORE any test
 * runs: under AUTHENTICATION_TYPE: OIDC, a password POST to /api/v1/signin
 * (used by the RawApiClient fixtures) makes Quay ROPC-validate against
 * Keycloak and create the user bound to the internal `oidc` login service
 * with service_ident = username. Once that row exists, the browser login for
 * the real provider (`someoidc`, service_ident = sub) can neither match nor
 * re-create the user ("The e-mail address ... is already associated"), and
 * every session for that user fails for the rest of the run.
 *
 * Running the browser login first wins the race: the later password signins
 * then fail harmlessly with 403 "Email has already been used" and leave the
 * (someoidc, sub) binding intact.
 *
 * Exits 0 when every user logged in (or when Quay is not in OIDC mode).
 */
import {chromium} from '@playwright/test';

const BASE_URL = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:8080';
const USERS = JSON.parse(process.env.OIDC_WARMUP_USERS || 'null') || [
  {username: 'admin_oidc', password: 'password'},
  {username: 'testuser_oidc', password: 'password'},
  {username: 'readonly_oidc', password: 'password'},
  {username: 'sync_oidc', password: 'password'},
];

async function loginOne(browser, username, password, config) {
  const context = await browser.newContext({ignoreHTTPSErrors: true});
  try {
    const provider = (config.external_login || [])[0];
    if (!provider) throw new Error('no external login provider in /config');

    // CSRF token from an anonymous session (same dance as loginUser).
    const pre = await context.request.get(`${BASE_URL}/csrf_token`);
    const csrf = (await pre.json()).csrf_token;
    const loginResponse = await context.request.post(
      `${BASE_URL}/api/v1/externallogin/${provider.id}`,
      {data: {kind: 'login'}, headers: {'X-CSRF-Token': csrf}},
    );
    if (!loginResponse.ok()) {
      throw new Error(`externallogin ${loginResponse.status()}`);
    }
    const {auth_url: authUrl} = await loginResponse.json();

    const page = await context.newPage();
    await page.goto(authUrl);
    await page.waitForURL(/.*realms\/.*/, {timeout: 20000});
    await page.fill('#username', username);
    await page.fill('#password', password);
    await page.click('#kc-login');
    // A successful login lands back on Quay; /oauth-error means the identity
    // is already poisoned (stale binding from an earlier run).
    await page.waitForURL(
      (url) =>
        url.origin === new URL(BASE_URL).origin &&
        !url.pathname.includes('/realms/'),
      {timeout: 20000},
    );
    if (page.url().includes('/oauth-error')) {
      throw new Error(`landed on oauth-error (${page.url()})`);
    }
  } finally {
    await context.close();
  }
}

(async () => {
  const configResponse = await fetch(`${BASE_URL}/config`);
  const config = await configResponse.json();
  if (config.config?.AUTHENTICATION_TYPE !== 'OIDC') {
    console.log('[warmup] not in OIDC mode; skipping');
    process.exit(0);
  }

  const browser = await chromium.launch();
  let failed = 0;
  for (const {username, password} of USERS) {
    try {
      await loginOne(browser, username, password, config);
      console.log(`[warmup] ${username}: OK`);
    } catch (err) {
      failed++;
      console.error(`[warmup] ${username}: FAILED — ${err.message}`);
    }
  }
  await browser.close();
  if (failed) {
    console.error(
      `[warmup] ${failed} user(s) failed. If the error is oauth-error, ` +
        'the identity binding is stale: reset with ./run.sh clean (drops the ' +
        'Quay DB and Keycloak data together).',
    );
    process.exit(1);
  }
})();
