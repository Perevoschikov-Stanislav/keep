/* Read-only navigation regression against the local k3d lab and real SSO. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const root = path.resolve(__dirname, '..');
const run = process.env.KEEP_CORE_RUN_DIR || path.join(root, '.lab-work/core-usability', fs.readFileSync(path.join(root, '.lab-work/core-usability/CURRENT'), 'utf8').trim().split('/').pop());
assert.ok(path.resolve(run).startsWith(path.join(root, '.lab-work') + path.sep), 'Artifacts must stay inside the fork');
const tmp = path.join(root, '.lab-work/browser-tmp');
fs.mkdirSync(tmp, { recursive: true });
Object.assign(process.env, { TMPDIR: tmp, TMP: tmp, TEMP: tmp });
const { chromium } = require('../keep-ui/node_modules/playwright');
const kube = ['--context', 'k3d-local', '--cache-dir=' + path.join(root, '.lab-work/kube-cache'), '-n', 'keep-lab'];
const cm = JSON.parse(execFileSync('kubectl', [...kube, 'get', 'cm', 'keycloak-realm', '-o', 'json'], { encoding: 'utf8' }));
const realm = Object.values(cm.data).map(JSON.parse).find(v => v.realm === 'core');
const origin = 'http://localhost:8000';
const checks = [], requests = [], errors = [];
const redact = value => String(value).split('Call log:')[0].replace(/[\w.+-]+@[\w.-]+\.[\w]+/g, '[email]').slice(0, 400);
let step = 'launch';
let page;
(async () => {
  const browser = await chromium.launch({ executablePath: process.env.KEEP_LAB_CHROME || path.join(os.homedir(), '.cache/ms-playwright/chromium-1134/chrome-linux/chrome'), headless: true, args: ['--no-sandbox'] });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  try {
    page = await context.newPage();
    await page.addInitScript(() => {
      window.navigationChanges = [];
      for (const name of ['pushState', 'replaceState']) {
        const original = history[name].bind(history);
        history[name] = function (...args) {
          const url = new URL(args[2] || location.href, location.href);
          window.navigationChanges.push({ method: name, path: url.pathname, view: url.searchParams.get('view') });
          return original(...args);
        };
      }
    });
    page.on('pageerror', error => errors.push(redact(error.message)));
    page.on('request', request => {
      const url = new URL(request.url());
      if (url.pathname === '/v2/incidents' && request.method() === 'GET') requests.push({ cel: url.searchParams.get('cel'), limit: url.searchParams.get('limit') });
    });
    step = 'login';
    await page.goto(origin + '/incidents', { waitUntil: 'domcontentloaded' });
    const user = realm.users.find(v => v.username === 'it-admin');
    await page.locator('#username').fill(user.username);
    await page.locator('#password').fill(user.credentials.find(v => v.type === 'password').value);
    await Promise.all([page.waitForNavigation({ waitUntil: 'domcontentloaded' }), page.locator('#kc-login').click()]);
    await page.goto(origin + '/incidents', { waitUntil: 'domcontentloaded' });
    await page.getByTestId('incident-view-all-link').waitFor();
    const response = await context.request.get(origin + '/v2/incidents/views');
    assert.equal(response.status(), 200);
    const views = await response.json();
    const teams = views.filter(v => v.id !== 'all');
    assert.ok(teams.length >= 3, 'Need the three existing lab views');
    for (const view of [...teams, ...teams.slice().reverse()]) {
      for (const target of [view, views.find(v => v.id === 'all')]) {
        step = 'click ' + target.name;
        const before = requests.length;
        await page.getByTestId('incident-view-' + target.id + '-link').click();
        await page.waitForFunction(id => (new URLSearchParams(location.search).get('view') || 'all') === id, target.id, { timeout: 8000 });
        await page.waitForTimeout(600);
        const selected = await page.locator('[data-testid^="incident-view-"][aria-current="page"]').getAttribute('data-testid');
        assert.equal(selected, 'incident-view-' + target.id + '-link');
        const state = { view: target.id, url: new URL(page.url()).pathname + new URL(page.url()).search, requests: requests.slice(before) };
        checks.push(state);
        console.log(JSON.stringify(state));
      }
    }
    assert.equal(errors.length, 0, errors.join('; '));
    fs.writeFileSync(path.join(run, 'incident-views-check.json'), JSON.stringify({ passed: true, checks, errors }, null, 2));
  } catch (error) {
    const navigation = await page?.evaluate(() => window.navigationChanges).catch(() => null);
    const current = page ? new URL(page.url()) : null;
    fs.writeFileSync(path.join(run, 'incident-views-check.json'), JSON.stringify({ passed: false, step, error: redact(error.message), url: current && { path: current.pathname, view: current.searchParams.get('view') }, navigation, checks, errors }, null, 2));
    throw error;
  } finally { await context.close(); await browser.close(); }
})().catch(error => { console.error(step + ': ' + redact(error.message)); process.exitCode = 1; });
