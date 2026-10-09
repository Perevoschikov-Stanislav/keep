const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const work = path.join(root, '.lab-work');
const run = process.env.KEEP_HYDRATION_RUN_DIR || path.join(work, 'incident-hydration', new Date().toISOString().replace(/[-:]/g, '').replace(/\.\d+Z$/, 'Z'));
assert.ok(path.resolve(run).startsWith(work + path.sep), 'Artifacts must stay in .lab-work');
const browserTmp = path.join(work, 'browser-tmp');
fs.mkdirSync(run, { recursive: true, mode: 0o700 });
fs.mkdirSync(browserTmp, { recursive: true });
Object.assign(process.env, { TMPDIR: browserTmp, TMP: browserTmp, TEMP: browserTmp });
const { chromium } = require('../keep-ui/node_modules/playwright');
const kube = ['--context', 'k3d-local', '--cache-dir=' + path.join(run, 'kube-cache'), '-n', 'keep-lab'];
const cm = JSON.parse(execFileSync('kubectl', [...kube, 'get', 'configmap', 'keycloak-realm', '-o', 'json'], { encoding: 'utf8' }));
const realm = Object.values(cm.data).map(JSON.parse).find(value => value.realm === 'core');
assert.ok(realm, 'Missing local lab realm');
const origin = 'http://localhost:8000';
const redact = value => String(value).replace(/Bearer\s+[^\s]+/gi, 'Bearer [redacted]').replace(/[\w.+-]+@[\w.-]+\.[\w]+/g, '[email]').slice(0, 500);
const findings = [];
let currentStep = 'launch';
const save = passed => fs.writeFileSync(path.join(run, 'hydration-verification.json'), JSON.stringify({ passed, findings }, null, 2));

(async () => {
  const browser = await chromium.launch({
    executablePath: process.env.KEEP_LAB_CHROME || process.env.KEEP_BROWSER_EXECUTABLE || path.join(os.homedir(), '.cache/ms-playwright/chromium-1134/chrome-linux/chrome'),
    headless: true,
    args: ['--no-sandbox'],
  });
  try {
    for (const username of ['it-admin', 'ops-l1', 'viewer', 'scanner']) {
      for (const timezoneId of ['UTC', 'Europe/Moscow']) {
        currentStep = username + '/' + timezoneId + '/login';
        const context = await browser.newContext({ timezoneId, viewport: { width: 1440, height: 1000 } });
        try {
          const page = await context.newPage();
          const errors = [];
          let commands = 0;
          page.on('pageerror', error => errors.push(redact(error.message)));
          page.on('console', message => {
            if (message.type() === 'error' && /hydrat|Minified React error|server rendered HTML/i.test(message.text())) errors.push(redact(message.text()));
          });
          page.on('request', request => {
            if (request.method() === 'POST' && new URL(request.url()).pathname.endsWith('/commands')) commands++;
          });
          await page.goto(origin, { waitUntil: 'domcontentloaded', timeout: 30000 });
          const user = realm.users.find(value => value.username === username);
          assert.ok(user, 'Missing existing lab user');
          await page.locator('#username').fill(username);
          await page.locator('#password').fill(user.credentials.find(value => value.type === 'password').value);
          await Promise.all([page.waitForNavigation({ waitUntil: 'domcontentloaded', timeout: 30000 }), page.locator('#kc-login').click()]);
          const permissionsResponse = await context.request.get(origin + '/v2/auth/users/me/permissions');
          assert.equal(permissionsResponse.status(), 200);
          const permissions = await permissionsResponse.json();
          const incidentsResponse = await context.request.get(origin + '/v2/incidents?limit=1000');
          assert.equal(incidentsResponse.status(), 200);
          const incidents = await incidentsResponse.json();
          const incident = incidents.items.find(value => value.alerts_count > 1 && !value.is_candidate);
          assert.ok(incident, 'Need an existing populated incident visible to this user');
          const base = origin + '/incidents/' + encodeURIComponent(incident.id);
          const entries = ['direct', 'reload', 'redirect', 'notification', 'client_navigation'];
          for (const entry of entries) {
            currentStep = username + '/' + timezoneId + '/' + entry;
            if (entry === 'reload') await page.reload({ waitUntil: 'domcontentloaded', timeout: 30000 });
            else if (entry === 'client_navigation') {
              await page.getByRole('tab', { name: 'Activity', exact: true }).click();
              await page.waitForURL('**/activity');
              await page.getByRole('tab', { name: /^Alerts(?:\s+\d+)?$/ }).click();
              await page.waitForURL('**/alerts');
            } else {
              const suffix = entry === 'direct' ? '/alerts' : entry === 'notification' ? '?command=ack&revision=' + (incident.lifecycle?.revision ?? 0) : '';
              await page.goto(base + suffix, { waitUntil: 'domcontentloaded', timeout: 30000 });
            }
            await page.getByRole('link', { name: 'All Incidents', exact: true }).waitFor();
            await page.getByRole('combobox', { name: 'Rows per page' }).waitFor();
            if (entry === 'notification') {
              await page.getByText('Confirm notification action', { exact: true }).waitFor();
              await page.waitForFunction(disabled => [...document.querySelectorAll('button')].some(button => button.textContent.trim() === 'Acknowledge' && button.disabled === disabled), permissions.role === 'viewer');
            }
            await page.waitForTimeout(800);
            assert.equal(errors.length, 0, currentStep + ': ' + errors.join('; '));
            assert.equal(commands, 0, 'Opening an incident submitted a command');
            findings.push({ user: username, role: permissions.role, timezone: timezoneId, entry, hydration_errors: 0 });
            save(false);
          }
          currentStep = username + '/' + timezoneId + '/page_size';
          await Promise.all([
            page.waitForResponse(response => {
              const url = new URL(response.url());
              return url.pathname.endsWith('/incidents/' + incident.id + '/alerts') && url.searchParams.get('limit') === '50' && response.status() === 200;
            }),
            page.getByRole('combobox', { name: 'Rows per page' }).selectOption('50'),
          ]);
          assert.equal(await page.getByRole('combobox', { name: 'Rows per page' }).inputValue(), '50');
          assert.equal(errors.length, 0, errors.join('; '));
          assert.equal(commands, 0);
          findings.push({ user: username, role: permissions.role, timezone: timezoneId, entry: 'page_size', selected: 50, hydration_errors: 0 });
          save(false);
          console.log(JSON.stringify({ user: username, role: permissions.role, timezone: timezoneId, opens: entries.length, page_size: 'ok', browser_errors: errors.length, commands }));
        } finally { await context.close(); }
      }
    }
    save(true);
  } finally { await browser.close(); }
})().catch(error => {
  fs.writeFileSync(path.join(run, 'hydration-failure.json'), JSON.stringify({ step: currentStep, error: redact(error.message) }, null, 2));
  console.error(currentStep + ': ' + redact(error.message));
  process.exitCode = 1;
});
