/* Actual local Keycloak SSO and engineer journeys after the Enterprise core cutover. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const root = path.resolve(__dirname, '..');
const run = process.env.KEEP_CORE_RUN_DIR || fs.readFileSync(path.join(root, '.lab-work/incident-core-upgrade/CURRENT'), 'utf8').trim();
assert.ok(path.resolve(run).startsWith(path.join(root, '.lab-work') + path.sep), 'Artifacts must stay inside the fork');
const tmp = path.join(root, '.lab-work/browser-tmp');
fs.mkdirSync(tmp, { recursive: true });
Object.assign(process.env, { TMPDIR: tmp, TMP: tmp, TEMP: tmp });
const { chromium } = require('../keep-ui/node_modules/playwright');
function browserExecutable() {
  if (process.env.KEEP_LAB_CHROME) return process.env.KEEP_LAB_CHROME;
  const registered = chromium.executablePath();
  if (fs.existsSync(registered)) return registered;
  const cache = process.env.PLAYWRIGHT_BROWSERS_PATH || path.join(os.homedir(), '.cache/ms-playwright');
  const candidates = fs.existsSync(cache) ? fs.readdirSync(cache)
    .filter(name => /^chromium-\d+$/.test(name))
    .sort((a, b) => b.localeCompare(a, undefined, { numeric: true }))
    .flatMap(name => ['chrome-linux', 'chrome-linux64'].map(dir => path.join(cache, name, dir, 'chrome'))) : [];
  return candidates.find(candidate => fs.existsSync(candidate)) || registered;
}
const origin = 'http://localhost:8000';
const kube = ['--context', 'k3d-local', '--cache-dir=' + path.join(root, '.lab-work/kube-cache'), '-n', 'keep-lab'];
const cm = JSON.parse(execFileSync('kubectl', [...kube, 'get', 'cm', 'keycloak-realm', '-o', 'json'], { encoding: 'utf8' }));
const realm = Object.values(cm.data).map(JSON.parse).find(v => v.realm === 'core');
const credentials = JSON.parse(execFileSync('kubectl', [...kube, 'get', 'secret', 'keep-mm-bridge', '-o', 'json'], { encoding: 'utf8' }));
const mmToken = Buffer.from(credentials.data.MM_BOT_TOKEN, 'base64').toString();
const fixtures = JSON.parse(fs.readFileSync(path.join(run, 'live-incidents.json'), 'utf8'));
const target = fixtures.find(v => v.rule === 'workload');
const foreign = fixtures.find(v => v.team === 'it');
assert.match(target.incident_id, /^[a-f0-9-]{36}$/);
assert.match(target.fingerprint, /^core-live-[a-z0-9]+-workload$/);
const checks = [], errors = [];
let step = 'launch';
let keepalive;
let lastPage;
const redact = v => String(v).split('Call log:')[0].replace(/Bearer\s+\S+/gi, 'Bearer [redacted]').replace(/[\w.+-]+@[\w.-]+\.[\w]+/g, '[email]').slice(0, 400);
function check(label, ok) { checks.push({ check: label, passed: !!ok }); assert.ok(ok, label); console.log('PASS ' + label); }
async function api(context, route, method = 'GET', data, expected = 200) {
  const r = await context.request.fetch(origin + '/v2' + route, { method, maxRetries: method === 'GET' ? 1 : 0, ...(data === undefined ? {} : { data }) });
  if (r.status() !== expected) fs.writeFileSync(path.join(run, 'browser-api-failure.json'),
    JSON.stringify({ method, route, status: r.status(), response: await r.text() }, null, 2));
  assert.equal(r.status(), expected, method + ' ' + route);
  const text = await r.text(); return text ? JSON.parse(text) : null;
}
async function eventually(label, action, timeout = 25000) {
  const until = Date.now() + timeout;
  while (Date.now() < until) { const v = await action(); if (v) { check(label, true); return v; } await new Promise(r => setTimeout(r, 400)); }
  check(label, false);
}
async function screenshot(page, name) {
  await page.evaluate(() => {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) walker.currentNode.textContent = walker.currentNode.textContent.replace(/[\w.+-]+@[\w.-]+\.[\w]+/g, '[email]');
  });
  await page.screenshot({ path: path.join(run, name + '.png'), fullPage: true });
}
async function mmPost() {
  const r = await fetch('http://localhost:8065/api/v4/posts/' + target.post_id, { headers: { Authorization: 'Bearer ' + mmToken } });
  assert.equal(r.status, 200); return r.json();
}
const backendBefore = JSON.parse(execFileSync('kubectl', [...kube, 'get', 'deployment', 'keep-backend', '-o', 'json'], { encoding: 'utf8' }));
const labKey = backendBefore.spec.template.spec.containers[0].env.find(v => v.name === 'KEEP_DEFAULT_API_KEYS').value.split(':').slice(2).join(':');
const sourceEvent = JSON.parse(fs.readFileSync(path.join(run, 'live-events.json'), 'utf8')).find(v => v.fingerprint === target.fingerprint);
async function directIncident() {
  const response = await fetch('http://localhost:8088/incidents/' + target.incident_id, { headers: { 'X-API-KEY': labKey } });
  assert.equal(response.status, 200); return response.json();
}
async function pulse() {
  // This direct synthetic webhook is absent from the real local AM inventory.
  // A changing diagnostic label keeps it fresh throughout the UI journey.
  const event = structuredClone(sourceEvent); event.labels.lab_ui_pulse = String(Date.now());
  event.annotations.runbook_url = 'https://runbook.lab.invalid/workload';
  const replica = structuredClone(event); replica.fingerprint += '-replica'; replica.labels.pod = 'catalog-abcde-fghij';
  replica.annotations.description = 'Replica needs investigation';
  const response = await fetch('http://localhost:8088/alerts/event/prometheus', { method: 'POST',
    headers: { 'X-API-KEY': labKey, 'Content-Type': 'application/json' }, body: JSON.stringify({ alerts: [event, replica] }) });
  assert.equal(response.status, 202);
  return event.labels.lab_ui_pulse;
}
function sourceApplied(stamp) {
  assert.match(stamp, /^\d+$/);
  const sql = "SELECT count(*) FROM incident i JOIN incidentcorrelationgroup g ON g.id=i.lifecycle_context->>'group_id' " +
    "JOIN alert a ON a.tenant_id=i.tenant_id AND replace(g.lifecycle_state->'members'->a.fingerprint->>'id','-','')=replace(a.id::text,'-','') " +
    "WHERE i.id='" + target.incident_id + "' AND a.fingerprint IN ('" + target.fingerprint + "','" + target.fingerprint + "-replica') " +
    "AND a.event->'labels'->>'lab_ui_pulse'='" + stamp + "'";
  return execFileSync('kubectl', [...kube, 'exec', 'deployment/keep-postgres', '--', 'psql', '-U', 'keep', '-d', 'keep', '-tAc', sql],
    { encoding: 'utf8' }).trim() === '2';
}
function canonicalState() {
  // Flapping in the API projection depends on the clock; compare stored state.
  const sql = "SELECT json_build_object('status',status,'assignee',assignee,'team_id',team_id,'lifecycle',lifecycle_context) " +
    "FROM incident WHERE id='" + target.incident_id + "'";
  return JSON.parse(execFileSync('kubectl', [...kube, 'exec', 'deployment/keep-postgres', '--', 'psql', '-U', 'keep', '-d', 'keep', '-tAc', sql],
    { encoding: 'utf8' }));
}
function sourceCount() {
  return Number(execFileSync('kubectl', [...kube, 'exec', 'deployment/keep-postgres', '--', 'psql', '-U', 'keep', '-d', 'keep', '-tAc',
    "SELECT count(*) FROM alert WHERE fingerprint IN ('" + target.fingerprint + "','" + target.fingerprint + "-replica')"], { encoding: 'utf8' }).trim());
}
function silenceObserved() {
  return execFileSync('kubectl', [...kube, 'exec', 'deployment/keep-postgres', '--', 'psql', '-U', 'keep', '-d', 'keep', '-tAc',
    "SELECT notification_context->>'silence_blocked' FROM incident WHERE id='" + target.incident_id + "'"], { encoding: 'utf8' }).trim() === 'true';
}
async function login(browser, username, url = origin) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, timezoneId: 'Europe/Moscow' });
  const page = await context.newPage();
  lastPage = page;
  const redirects = [];
  page.on('response', response => {
    if (![301, 302, 303, 307, 308].includes(response.status())) return;
    const clean = raw => {
      const value = new URL(raw, origin);
      return { path: value.pathname, query_keys: [...value.searchParams.keys()],
        command: value.searchParams.get('command'), revision: value.searchParams.get('revision'),
        rd: value.searchParams.get('rd'), callbackUrl: value.searchParams.get('callbackUrl') };
    };
    redirects.push({ from: clean(response.url()), to: clean(response.headers().location || origin) });
  });
  page.on('pageerror', error => errors.push({ user: username, message: redact(error.message) }));
  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 });
  const user = realm.users.find(u => u.username === username);
  await page.locator('#username').fill(username);
  await page.locator('#password').fill(user.credentials.find(c => c.type === 'password').value);
  await Promise.all([page.waitForNavigation({ waitUntil: 'domcontentloaded', timeout: 30000 }), page.locator('#kc-login').click()]);
  fs.writeFileSync(path.join(run, 'browser-redirects-' + username + '.json'), JSON.stringify(redirects, null, 2));
  return { context, page };
}
async function command(context, page, name, label) {
  const current = await api(context, '/incidents/' + target.incident_id);
  await page.goto(origin + '/incidents/' + target.incident_id + '?command=' + name + '&revision=' + current.lifecycle.revision);
  await page.getByText('Confirm notification action', { exact: true }).waitFor();
  const [response] = await Promise.all([
    page.waitForResponse(r => r.request().method() === 'POST' && r.url().endsWith('/commands')),
    page.getByRole('button', { name: label, exact: true }).click(),
  ]);
  assert.equal(response.status(), 200);
}

(async () => {
  const browser = await chromium.launch({ executablePath: browserExecutable(), headless: true, args: ['--no-sandbox'] });
  const contexts = [];
  let silence;
  try {
    const previousFile = path.join(run, 'browser-checks.json');
    const previous = fs.existsSync(previousFile) ? JSON.parse(fs.readFileSync(previousFile, 'utf8')) : {};
    if (previous.silence_id) {
      const headers = { 'X-API-KEY': labKey, 'Content-Type': 'application/json' };
      const response = await fetch('http://localhost:8088/silences/' + previous.silence_id, { headers });
      assert.equal(response.status, 200);
      const prior = await response.json();
      assert.ok(prior.comment.startsWith('Enterprise live verification') && prior.selector.incident_ids?.includes(target.incident_id), 'Only cancel an owned synthetic fixture rule');
      if (['active', 'scheduled'].includes(prior.state)) {
        const cancelled = await fetch('http://localhost:8088/silences/' + prior.id + '/cancel', { method: 'POST', headers,
          body: JSON.stringify({ schema_version: 1, client_request_id: crypto.randomUUID(), expected_revision: prior.revision, reason: 'Retry owned local UI verification', correlation_id: null }) });
        assert.equal(cancelled.status, 200);
      }
    }
    await pulse();
    // A failed prior journey may leave a skipped projection. Start this owned
    // fixture with a new canonical update, without replaying suppressed history.
    for (const status of ['acknowledged', 'firing']) {
      const incident = await directIncident();
      const response = await fetch('http://localhost:8088/incidents/' + target.incident_id + '/status', { method: 'POST',
        headers: { 'X-API-KEY': labKey, 'Content-Type': 'application/json' },
        body: JSON.stringify({ status, expected_revision: incident.lifecycle.revision }) });
      assert.equal(response.status, 200);
    }
    keepalive = setInterval(() => pulse().catch(() => errors.push({ message: 'Local fixture keepalive failed' })), 20000);
    await eventually('synthetic UI fixture is active', async () => ['firing', 'acknowledged'].includes((await directIncident()).status));
    await eventually('actual Mattermost action link matches the current revision', async () => {
      const row = await directIncident(); const post = await mmPost();
      return post.props.attachments[0].footer.includes('command=ack&revision=' + row.lifecycle.revision + ')');
    }, 150000);
    step = 'Mattermost link through real SSO';
    const post = await mmPost();
    const match = post.props.attachments[0].footer.match(/\[Acknowledge\]\(([^)]+)\)/);
    assert.ok(match, 'Actual Mattermost post has Keep action link');
    const ops = await login(browser, 'ops-l1', match[1]); contexts.push(ops.context);
    const { context, page } = ops;
    let commands = 0;
    page.on('request', r => { if (r.method() === 'POST' && r.url().endsWith('/commands')) commands++; });
    await page.getByText('Confirm notification action', { exact: true }).waitFor();
    await page.waitForTimeout(600);
    check('Mattermost action survives Keycloak SSO without executing', page.url().includes(target.incident_id) && commands === 0);
    const permissions = await api(context, '/auth/users/me/permissions');
    fs.writeFileSync(path.join(run, 'browser-permissions.json'), JSON.stringify(permissions, null, 2));
    check('SSO user is a responder', permissions.role === 'responder');
    step = 'acknowledge';
    fs.writeFileSync(path.join(run, 'browser-action-text.txt'), redact(await page.locator('body').innerText()));
    fs.writeFileSync(path.join(run, 'browser-action-debug.json'), JSON.stringify({ url: page.url(),
      api_revision: (await api(context, '/incidents/' + target.incident_id)).lifecycle?.revision,
      rendered: await page.getByRole('button', { name: 'Acknowledge', exact: true }).evaluate(element => {
        let fiber = element[Object.keys(element).find(key => key.startsWith('__reactFiber'))];
        for (; fiber; fiber = fiber.return) if (fiber.memoizedProps?.incident) {
          const i = fiber.memoizedProps.incident; return { id: i.id, team_id: i.team_id, revision: i.lifecycle?.revision };
        }
        return null;
      }) }, null, 2));
    await screenshot(page, 'notification-confirmation');
    await eventually('responder ACK confirmation is enabled', async () => !(await page.getByRole('button', { name: 'Acknowledge', exact: true }).isDisabled()));
    const [response] = await Promise.all([
      page.waitForResponse(r => r.request().method() === 'POST' && r.url().endsWith('/commands')),
      page.getByRole('button', { name: 'Acknowledge', exact: true }).click(),
    ]);
    assert.equal(response.status(), 200);
    await eventually('confirmed ACK changes canonical incident once', async () => (await api(context, '/incidents/' + target.incident_id)).status === 'acknowledged');
    check('one human command was submitted', commands === 1);
    step = 'assign';
    await command(context, page, 'assign', 'Assign to me');
    check('assign uses the authenticated human', !!(await api(context, '/incidents/' + target.incident_id)).assignee);
    step = 'native unacknowledge';
    const native = page.getByRole('combobox', { name: 'Incident status' });
    fs.writeFileSync(path.join(run, 'native-status-debug.json'), JSON.stringify(await native.evaluate(element => {
      let fiber = element[Object.keys(element).find(key => key.startsWith('__reactFiber'))];
      for (; fiber; fiber = fiber.return) if (fiber.memoizedProps?.incidentId) {
        const p = fiber.memoizedProps; return { incidentId: p.incidentId, teamId: p.teamId, value: p.value, disabled: element.disabled };
      }
      return { disabled: element.disabled };
    }), null, 2));
    check('native incident status control is enabled for responder', !(await native.isDisabled()));
    const statusControl = native.locator('xpath=ancestor::div[contains(@class, "-control")][1]');
    await statusControl.click();
    await page.getByRole('option', { name: 'Firing', exact: true }).waitFor();
    const nativeResponse = page.waitForResponse(r => r.request().method() === 'POST' && r.url().endsWith('/incidents/' + target.incident_id + '/status'));
    await page.getByRole('option', { name: 'Firing', exact: true }).click();
    const nativeResult = await nativeResponse;
    fs.writeFileSync(path.join(run, 'native-status-response.json'), JSON.stringify({ status: nativeResult.status(),
      body: await nativeResult.json() }, null, 2));
    assert.equal(nativeResult.status(), 200);
    await eventually('native Keep control can unacknowledge', async () => (await api(context, '/incidents/' + target.incident_id)).status === 'firing');
    const presentation = page.getByRole('region', { name: 'Normalized object' });
    await eventually('Keep UI shows both objects and descriptions', async () => {
      const text = await presentation.innerText();
      // Incident overview renders the generated description in Summary above
      // the field card; that card hides its duplicate description.
      const description = await page.getByText('Summary', { exact: true }).locator('..').innerText();
      return text.includes('catalog-abcde-fghij') && description.includes('Replica needs investigation') && description.includes('Lab-only engineer scenario workload');
    });
    check('Keep UI exposes source Prometheus and runbook links', await presentation.getByRole('link', { name: 'Prometheus', exact: true }).count() === 1 && await presentation.getByRole('link', { name: 'Runbook', exact: true }).count() === 1);
    step = 'silence from alert';
    const sourceAlerts = await api(context, '/incidents/' + target.incident_id + '/alerts');
    const sourceAlert = sourceAlerts.items.find(alert => alert.fingerprint === target.fingerprint);
    assert.ok(sourceAlert, 'Canonical incident contains the source alert');
    await page.goto(origin + '/incidents/' + target.incident_id + '/alerts');
    const alertRow = page.getByRole('row').filter({ hasText: sourceAlert.name })
      .filter({ hasText: sourceAlert.description || sourceAlert.name }).first();
    await alertRow.click();
    const alertSidebar = page.getByRole('dialog').filter({ has: page.getByRole('button', { name: 'Silence', exact: true }) });
    await alertSidebar.getByRole('button', { name: 'Silence', exact: true }).click();
    await alertSidebar.waitFor({ state: 'hidden' });
    const alertDialog = page.getByRole('dialog').filter({ has: page.getByLabel('Reason / Comment') });
    await alertDialog.getByText('Team: ' + target.team, { exact: true }).waitFor();
    await alertDialog.getByLabel('Reason / Comment').fill('Local alert menu verification');
    const alertCreated = page.waitForResponse(r => r.url().endsWith('/v2/silences') && r.request().method() === 'POST');
    await alertDialog.getByRole('button', { name: 'Apply Silence' }).click();
    const alertResponse = await alertCreated; assert.equal(alertResponse.status(), 201);
    const alertSilence = (await alertResponse.json()).result;
    check('alert menu creates a team-owned canonical fingerprint silence', alertSilence.team_id === target.team && alertSilence.selector.fingerprints.join() === target.fingerprint);
    const alertRule = await api(context, '/silences/' + alertSilence.id);
    await api(context, '/silences/' + alertRule.id + '/cancel', 'POST', { schema_version: 1,
      client_request_id: crypto.randomUUID(), expected_revision: alertRule.revision,
      reason: 'Local alert verification complete', correlation_id: null });
    check('alert silence cancellation preserves incident status', (await api(context, '/incidents/' + target.incident_id)).status === 'firing');
    step = 'silence from incident';
    const beforeSilence = await api(context, '/incidents/' + target.incident_id);
    await page.goto(origin + '/incidents/' + target.incident_id + '?command=silence&revision=' + beforeSilence.lifecycle.revision);
    await page.getByRole('button', { name: 'Create silence', exact: true }).click();
    const dialog = page.getByRole('dialog');
    await dialog.getByLabel('Reason / Comment').fill('Enterprise live verification · planned service work');
    const created = page.waitForResponse(r => r.url().endsWith('/v2/silences') && r.request().method() === 'POST');
    await dialog.getByRole('button', { name: 'Apply Silence' }).click();
    const createdResponse = await created; assert.equal(createdResponse.status(), 201);
    silence = (await createdResponse.json()).result;
    check('incident silence uses canonical team and selector', silence.team_id === 'ops' && silence.selector.incident_ids[0] === target.incident_id);
    await page.goto(origin + '/silences');
    await page.getByRole('heading', { name: 'Silences Registry' }).waitFor();
    const row = page.getByRole('row').filter({ hasText: silence.comment }); await row.waitFor();
    check('registry displays silence created from incident', await row.count() === 1);
    step = 'edit silence in registry';
    await row.locator('button[title="Edit rule"]').click();
    await page.getByRole('dialog').getByLabel('Reason / Comment').fill('Enterprise live verification · edited service work');
    const editedResponse = page.waitForResponse(r => r.request().method() === 'PATCH' && r.url().includes('/v2/silences/'));
    await page.getByRole('button', { name: 'Save Changes' }).click();
    assert.equal((await editedResponse).status(), 200);
    await eventually('registry edit updates canonical reason', async () => (await api(context, '/silences/' + silence.id)).comment.endsWith('edited service work'));
    const finalRow = page.getByRole('row').filter({ hasText: 'Enterprise live verification · edited service work' });
    await finalRow.waitFor(); await screenshot(page, 'silences-desktop');
    step = 'silence service annotation';
    await eventually('Mattermost displays silence on existing incident post', async () => JSON.stringify((await mmPost()).props).includes(silence.id), 30000);
    const projection = (await mmPost()).props.keep_projection_revision;
    await command(context, page, 'ack', 'Acknowledge');
    const skipped = execFileSync('kubectl', [...kube, 'exec', 'deployment/keep-postgres', '--', 'psql', '-U', 'keep', '-d', 'keep', '-tAc',
      "SELECT count(*) FROM notificationdelivery WHERE state='skipped' AND context->>'incident_id'='" + target.incident_id + "'"], { encoding: 'utf8' }).trim();
    check('silence suppresses incident dispatch while ACK still works', Number(skipped) > 0 && (await mmPost()).props.keep_projection_revision === projection);
    step = 'cross-team and read-only protection';
    await api(context, '/incidents/' + foreign.incident_id, 'GET', undefined, 404);
    const fresh = await api(context, '/incidents/' + target.incident_id);
    await api(context, '/incidents/' + target.incident_id, 'DELETE', undefined, 403);
    check('responder cannot read another team or delete an incident', true);
    const viewer = await login(browser, 'viewer', origin + '/incidents/' + target.incident_id + '?command=ack&revision=' + fresh.lifecycle.revision); contexts.push(viewer.context);
    await viewer.page.getByText('Confirm notification action', { exact: true }).waitFor();
    check('viewer action confirmation is disabled', await viewer.page.getByRole('button', { name: 'Acknowledge', exact: true }).isDisabled());
    await api(viewer.context, '/incidents/' + target.incident_id + '/commands', 'POST', { schema_version: 1, client_request_id: crypto.randomUUID(), incident_id: target.incident_id, expected_revision: fresh.lifecycle.revision, command: 'ack', correlation_id: null }, 403);
    await viewer.page.goto(origin + '/silences');
    await viewer.page.getByRole('button', { name: 'New Silence Rule' }).waitFor();
    check('viewer cannot create silence in UI', await viewer.page.getByRole('button', { name: 'New Silence Rule' }).isDisabled());
    const scanner = await login(browser, 'scanner'); contexts.push(scanner.context);
    await api(scanner.context, '/incidents/' + target.incident_id, 'GET', undefined, 404);
    await api(scanner.context, '/incidents/' + foreign.incident_id);
    check('IT read-only user sees own team and cannot read OPS', (await api(scanner.context, '/auth/users/me/permissions')).role === 'viewer');
    step = 'teams and engineer navigation';
    await page.goto(origin + '/settings?tab=teams');
    await page.getByRole('tab', { name: 'Teams', exact: true }).click();
    await page.getByText('Active IaC configuration', { exact: true }).waitFor();
    const teams = await api(context, '/auth/teams');
    const policyFile = fs.existsSync(path.join(run, 'channels-policy-apply.json')) ? 'channels-policy-apply.json' : 'policy-apply.json';
    const generation = JSON.parse(fs.readFileSync(path.join(run, policyFile), 'utf8')).generation;
    check('Teams UI reflects active IaC and membership', teams.teams.map(t => t.id).join() === 'ops' && teams.configuration.generation === generation);
    check('Maintenance and paid navigation are hidden', (await page.getByRole('link', { name: 'Maintenance', exact: true }).count()) === 0 && (await page.getByRole('tab', { name: 'SSO', exact: true }).count()) === 0);
    await screenshot(page, 'teams-desktop');
    step = 'cancel silence and resolve';
    clearInterval(keepalive); keepalive = null;
    const beforeCancel = canonicalState();
    const rule = await api(context, '/silences/' + silence.id);
    await api(context, '/silences/' + silence.id + '/cancel', 'POST', { schema_version: 1, client_request_id: crypto.randomUUID(), expected_revision: rule.revision, reason: 'Local verification complete', correlation_id: null });
    await eventually('cancellation removes active Mattermost coverage', async () => !JSON.stringify((await mmPost()).props).includes('edited service work'), 30000);
    await eventually('cancellation sends the current incident without new ingestion', async () => (await mmPost()).props.keep_projection_revision > projection, 120000);
    check('cancellation preserves ACK, assignee and lifecycle revision', JSON.stringify(canonicalState()) === JSON.stringify(beforeCancel));
    step = 'short silence expiry';
    const expiryPulse = await pulse();
    await eventually('expiry fixture ingestion has settled', async () => sourceApplied(expiryPulse));
    const beforeExpiry = canonicalState();
    const createdShort = await api(context, '/silences', 'POST', { schema_version: 1, client_request_id: crypto.randomUUID(),
      team_id: target.team, selector: { kind: 'incident', incident_ids: [target.incident_id] }, starts_at: null,
      ends_at: new Date(Date.now() + 180000).toISOString(), comment: 'Enterprise short expiry verification', correlation_id: null }, 201);
    const short = createdShort.result;
    await eventually('worker durably observes the unchanged silenced incident', async () => silenceObserved(), 60000);
    const currentProjection = (await mmPost()).props.keep_projection_revision;
    await api(context, '/silences/' + short.id, 'PATCH', { schema_version: 1, client_request_id: crypto.randomUUID(),
      expected_revision: short.revision, changes: { ends_at: new Date(Date.now() + 10000).toISOString() }, correlation_id: null });
    const alertsBeforeExpiry = sourceCount();
    await eventually('short silence reaches canonical expired state', async () => (await api(context, '/silences/' + short.id)).state === 'expired', 120000);
    await eventually('expiry removes Mattermost silence coverage', async () => !JSON.stringify((await mmPost()).props).includes(short.id), 30000);
    await eventually('expiry sends one fresh projection on the same Mattermost post', async () => (await mmPost()).props.keep_projection_revision > currentProjection, 120000);
    const afterExpiry = canonicalState(), alertsAfterExpiry = sourceCount();
    fs.writeFileSync(path.join(run, 'browser-expiry-check.json'), JSON.stringify({ beforeExpiry, afterExpiry, alertsBeforeExpiry, alertsAfterExpiry }, null, 2));
    check('expiry requires no new source alert and preserves lifecycle', alertsAfterExpiry === alertsBeforeExpiry &&
      JSON.stringify(afterExpiry) === JSON.stringify(beforeExpiry));
    check('expired silence no longer covers the incident', (await api(context, '/incidents/' + target.incident_id)).silence.coverage === 'none');
    await command(context, page, 'resolve', 'Resolve');
    check('responder can resolve without delete scope', (await api(context, '/incidents/' + target.incident_id)).status === 'resolved');
    check('no React runtime or hydration error in engineer journeys', errors.length === 0);
    fs.writeFileSync(path.join(run, 'browser-checks.json'), JSON.stringify({ passed: true, checks, errors, silence_id: silence.id, visual_regression: 'INCONCLUSIVE: no baseline' }, null, 2));
  } catch (error) {
    fs.writeFileSync(path.join(run, 'browser-failure-detail.txt'), String(error.stack || error.message)
      .replace(/Bearer\s+\S+/gi, 'Bearer [redacted]')
      .replace(/[\w.+-]+@[\w.-]+\.[\w]+/g, '[email]').slice(0, 8000));
    if (lastPage && new URL(lastPage.url()).origin === origin && !new URL(lastPage.url()).pathname.startsWith('/signin')) {
      await screenshot(lastPage, 'browser-failure').catch(() => {});
    }
    fs.writeFileSync(path.join(run, 'browser-checks.json'), JSON.stringify({ passed: false, step, checks, errors, error: redact(error.message), silence_id: silence?.id }, null, 2));
    console.error('FAIL ' + step + ': ' + redact(error.message)); process.exitCode = 1;
  } finally { if (keepalive) clearInterval(keepalive); for (const context of contexts) await context.close(); await browser.close(); }
})();
