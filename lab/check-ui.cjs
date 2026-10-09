const { chromium } = require('../keep-ui/node_modules/playwright');
const { execFileSync } = require('node:child_process');
const assert = require('node:assert/strict');
const { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } = require('node:fs');
const { homedir } = require('node:os');
const path = require('node:path');
const { parse } = require('../keep-ui/node_modules/yaml');

function browserExecutable() {
  if (process.env.KEEP_LAB_CHROME) return process.env.KEEP_LAB_CHROME;
  const registered = chromium.executablePath();
  if (existsSync(registered)) return registered;
  const cache = process.env.PLAYWRIGHT_BROWSERS_PATH || path.join(homedir(), '.cache/ms-playwright');
  const candidates = existsSync(cache) ? readdirSync(cache)
    .filter(name => /^chromium-\d+$/.test(name))
    .sort((a, b) => b.localeCompare(a, undefined, { numeric: true }))
    .flatMap(name => ['chrome-linux', 'chrome-linux64'].map(dir => path.join(cache, name, dir, 'chrome'))) : [];
  return candidates.find(candidate => existsSync(candidate)) || registered;
}

const tempDir = path.resolve(__dirname, '../.lab-work');
mkdirSync(tempDir, { recursive: true });
process.env.TMPDIR = tempDir;
process.env.XDG_CACHE_HOME = path.join(tempDir, 'browser-cache');
process.env.XDG_CONFIG_HOME = path.join(tempDir, 'browser-config');
const kube = ['--context', 'k3d-local', `--cache-dir=${path.join(tempDir, 'kube-cache')}`, '-n', 'keep-lab'];
const cm = JSON.parse(execFileSync('kubectl', [...kube, 'get', 'configmap', 'keycloak-realm', '-o', 'json'], { encoding: 'utf8' }));
const realm = Object.values(cm.data).map(value => JSON.parse(value)).find(value => value.realm === 'core');
const origin = 'http://localhost:8000';
const safe = value => String(value).replace(/Bearer\s+[^\s]+/gi, 'Bearer [redacted]').replace(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi, '[email]').replace(/https?:\/\/[^\s)]+/g, value => value.split('?')[0]).slice(0, 300);
const policy = parse(readFileSync(path.join(__dirname, 'team-policy.yaml'), 'utf8'));
const memberships = parse(readFileSync(path.join(__dirname, 'keycloak-memberships.yaml'), 'utf8')).users;
const expectedRoles = Object.fromEntries(Object.entries(memberships).map(([username, groups]) => [
  username, ['admin', 'responder', 'viewer', 'noc'].find(role =>
    (policy.roles[role] || []).some(group => groups.includes(group))),
]));
const memberTeams = username => policy.teams.filter(team =>
  team.groups.some(group => memberships[username].includes(group))).map(team => team.id);
const visibleTeams = username => expectedRoles[username] === 'admin' || policy.visibility === 'all'
  ? null : policy.teams.filter(team =>
    (team.visible_to || [team.id]).some(id => memberTeams(username).includes(id))).map(team => team.id);
const activeCel = "is_candidate == false && (status in ['firing', 'acknowledged'])";
let currentStep = 'starting';

async function login(page, username) {
  const user = realm.users.find(value => value.username === username);
  assert.ok(user, `Missing lab user: ${username}`);
  await page.goto(origin, { waitUntil: 'domcontentloaded', timeout: 20000 });
  await page.locator('#username').fill(username);
  await page.locator('#password').fill(user.credentials.find(value => value.type === 'password').value);
  await Promise.all([
    page.waitForNavigation({ waitUntil: 'domcontentloaded', timeout: 20000 }),
    page.locator('#kc-login').click(),
  ]);
  await page.waitForTimeout(4000);
  assert.equal(new URL(page.url()).origin, origin, `${username}: login did not reach Keep`);
}

async function checkPermissions(context, username) {
  const response = await context.request.get(`${origin}/v2/auth/users/me/permissions`);
  assert.equal(response.status(), 200, `${username}: permissions API`);
  const permissions = await response.json();
  assert.equal(permissions.role, expectedRoles[username], `${username}: unexpected role`);
  assert.deepEqual(permissions.writable_teams,
    expectedRoles[username] === 'admin' ? null
      : ['responder', 'noc'].includes(expectedRoles[username]) ? memberTeams(username).sort() : [],
    `${username}: incorrect writable teams`);
  return permissions;
}

async function checkTeamVisibilityAndWriteDenial(context, username, incidents, permissions, fixtures, adminContext) {
  const allowed = visibleTeams(username);
  const isVisible = item => allowed === null || allowed.includes(item.team_id);
  assert.ok(incidents.items.every(isVisible), `${username}: foreign incident leaked`);
  const response = await context.request.get(`${origin}/v2/alerts?limit=1000`);
  assert.equal(response.status(), 200, `${username}: alerts API`);
  const alerts = await response.json();
  assert.ok(alerts.every(isVisible), `${username}: foreign alert leaked`);
  for (const team of allowed || policy.teams.map(item => item.id)) {
    assert.ok(incidents.items.some(item => item.team_id === team), `${username}: no visible incident fixture for ${team}`);
    assert.ok(alerts.some(item => item.team_id === team), `${username}: no visible alert fixture for ${team}`);
  }
  if (permissions.role === 'admin') return;

  const hiddenIncident = fixtures.incidents.find(item => !isVisible(item));
  const hiddenAlert = fixtures.alerts.find(item => !isVisible(item));
  if (allowed !== null) {
    assert.ok(hiddenIncident && hiddenAlert, `${username}: no hidden fixtures in lab`);
    assert.equal((await context.request.get(`${origin}/v2/incidents/${encodeURIComponent(hiddenIncident.id)}`)).status(), 404, `${username}: hidden incident must return 404`);
    assert.equal((await context.request.get(`${origin}/v2/alerts/${encodeURIComponent(hiddenAlert.fingerprint)}`)).status(), 404, `${username}: hidden alert must return 404`);
  }
  const forbiddenIncident = hiddenIncident || fixtures.incidents.find(item => !permissions.writable_teams.includes(item.team_id));
  const forbiddenAlert = hiddenAlert || fixtures.alerts.find(item => !permissions.writable_teams.includes(item.team_id));
  assert.ok(forbiddenIncident && forbiddenAlert, `${username}: no foreign fixtures in lab`);
  const incidentUrl = `${origin}/v2/incidents/${encodeURIComponent(forbiddenIncident.id)}`;
  const before = await (await adminContext.request.get(incidentUrl)).json();
  const assignment = await context.request.post(`${origin}/v2/incidents/${encodeURIComponent(forbiddenIncident.id)}/assign`);
  const hiddenStatus = item => !isVisible(item) && ['responder', 'noc'].includes(permissions.role) ? 404 : 403;
  const canUpdateIncident = permissions.scopes.some(scope => ['update:incident', 'update:*'].includes(scope));
  assert.equal(assignment.status(), !isVisible(forbiddenIncident) && canUpdateIncident ? 404 : 403, `${username}: foreign incident assignment must be denied`);
  const after = await (await adminContext.request.get(incidentUrl)).json();
  assert.equal(after.assignee, before.assignee, `${username}: denied assignment changed incident`);
  const alertAssignment = await context.request.post(`${origin}/v2/alerts/${encodeURIComponent(forbiddenAlert.fingerprint)}/assign/${encodeURIComponent(forbiddenAlert.lastReceived)}`);
  assert.equal(alertAssignment.status(), hiddenStatus(forbiddenAlert), `${username}: foreign alert assignment must be denied`);
  console.log(JSON.stringify({ user: username, visibility: policy.visibility, hiddenReads: allowed === null ? 'shared' : 'denied', foreignWrites: 'denied' }));
}

async function checkViews(page, context, username) {
  const response = await context.request.get(`${origin}/v2/incidents/views`);
  assert.equal(response.status(), 200, `${username}: incident views API`);
  const views = await response.json();
  assert.deepEqual(views.map(view => view.name), ['ALL', ...(policy.incident_views || []).map(view => view.name)]);
  await page.goto(`${origin}/incidents`, { waitUntil: 'domcontentloaded' });
  for (const view of views) {
    currentStep = `${username}: view ${view.name}`;
    const link = page.getByTestId(`incident-view-${view.id}-link`);
    await link.waitFor({ state: 'visible' });
    await link.click();
    await page.locator('p:visible').filter({ hasText: `Incidents · ${view.name}` }).first().waitFor({ state: 'visible' });
    await page.waitForTimeout(2200);
    assert.equal(new URL(page.url()).searchParams.get('view') || 'all', view.id, `${username}: view lost from URL`);
    assert.equal(await link.getAttribute('aria-current'), 'page', `${username}: view is not active`);
    assert.ok(!/Server Components render|An error occurred|Cannot read properties/i.test(await page.locator('body').innerText()), `${username}: view render failed`);
    const cel = [activeCel, view.cel].filter(Boolean).map(value => `(${value})`).join(' && ');
    const queryResponse = await context.request.get(`${origin}/v2/incidents?limit=100&cel=${encodeURIComponent(cel)}`);
    assert.equal(queryResponse.status(), 200, `${username}: ${view.name} query failed`);
    const data = await queryResponse.json();
    const tableIds = await page.locator('table a[href^="/incidents/"]').evaluateAll(links => links.map(link => new URL(link.href).pathname.split('/')[2]));
    if (data.count > 0) assert.ok(tableIds.length > 0, `${username}: ${view.name} table did not render incidents`);
    if (view.id !== 'all') {
      const allowed = new Set(data.items.map(item => item.id));
      assert.ok(tableIds.every(id => allowed.has(id)), `${username}: table did not apply ${view.name} filter`);
    }
    console.log(JSON.stringify({ user: username, view: view.name, count: data.count, rendered: 'ok' }));
  }
}

(async () => {
  const browser = await chromium.launch({ executablePath: browserExecutable(), headless: true, args: ['--no-sandbox'] });
  const results = [];
  try {
    const adminUsername = Object.keys(expectedRoles).find(username => expectedRoles[username] === 'admin');
    assert.ok(adminUsername, 'The lab needs an admin fixture account');
    const adminContext = await browser.newContext();
    await login(await adminContext.newPage(), adminUsername);
    const fixtures = {
      incidents: (await (await adminContext.request.get(`${origin}/v2/incidents?limit=1000`)).json()).items,
      alerts: await (await adminContext.request.get(`${origin}/v2/alerts?limit=1000`)).json(),
    };
    const usernames = process.env.KEEP_LAB_USERS?.split(',') || Object.keys(expectedRoles);
    for (const username of usernames) {
      assert.ok(expectedRoles[username], 'Unknown lab test user');
      currentStep = `${username}: login`;
      const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
      const page = await context.newPage();
      let errors = [];
      let failedResponses = [];
      let crashed = false;
      let frameNavigations = [];
      page.on('pageerror', error => errors.push(safe(error.message)));
      page.on('crash', () => { crashed = true; });
      page.on('framenavigated', frame => {
        if (frame === page.mainFrame()) frameNavigations.push(safe(frame.url()));
      });
      page.on('response', response => {
        if (response.status() === 401 || response.status() >= 500) {
          const url = new URL(response.url());
          failedResponses.push({ status: response.status(), path: url.pathname });
        }
      });
      await login(page, username);
      const sessionResponse = await context.request.get(`${origin}/api/auth/session`);
      const session = await sessionResponse.json();
      assert.equal(sessionResponse.status(), 200);
      assert.ok(session.user, `${username}: no UI session`);
      const pages = [];
      const incidentsResponse = await context.request.get(`${origin}/v2/incidents?limit=100`);
      assert.equal(incidentsResponse.status(), 200, `${username}: incidents API`);
      const incidents = await incidentsResponse.json();
      const permissions = await checkPermissions(context, username);
      const seenTeams = [...new Set(incidents.items.map(incident => incident.team_id))];
      await checkTeamVisibilityAndWriteDenial(context, username, incidents, permissions, fixtures, adminContext);
      console.log(JSON.stringify({ user: username, incidentTeams: seenTeams, role: permissions.role, writableTeams: permissions.writable_teams }));
      const authOnly = process.env.KEEP_LAB_AUTH_ONLY === '1';
      const routes = authOnly ? ['/incidents'] : ['/', '/incidents', '/alerts/feed'];
      if (!authOnly) {
        const samples = permissions.role === 'responder'
          ? policy.teams.map(team => incidents.items.find(item => item.team_id === team.id))
          : [incidents.items[0]];
        for (const sample of samples.filter(Boolean)) routes.push(`/incidents/${encodeURIComponent(sample.id)}`);
      }
      for (const route of routes) {
        currentStep = `${username}: ${route.startsWith('/incidents/') ? '/incidents/[id]' : route}`;
        errors = [];
        failedResponses = [];
        frameNavigations = [];
        const profiler = process.env.KEEP_LAB_PROFILE === '1' && route === '/alerts/feed'
          ? await context.newCDPSession(page) : null;
        if (profiler) {
          await profiler.send('Profiler.enable');
          await profiler.send('Profiler.start');
        }
        const navigation = await page.goto(`${origin}${route}`, { waitUntil: 'domcontentloaded', timeout: 20000 });
        await page.waitForTimeout(3500);
        let body;
        try {
          body = await page.locator('body').innerText();
        } catch (error) {
          console.log(JSON.stringify({ user: username, path: new URL(page.url()).pathname, crashed, frameNavigations, pageErrors: errors, failedResponses }));
          throw error;
        } finally {
          if (profiler) {
            const result = await Promise.race([
              profiler.send('Profiler.stop'),
              new Promise(resolve => setTimeout(() => resolve(null), 5000)),
            ]).catch(() => null);
            if (result?.profile) {
              for (const node of result.profile.nodes) node.callFrame.url = node.callFrame.url.split('?')[0];
              writeFileSync(path.join(tempDir, 'feed.cpuprofile'), JSON.stringify(result.profile));
            }
            await profiler.detach();
          }
        }
        const report = {
          page: route.startsWith('/incidents/') ? '/incidents/[id]' : route,
          status: navigation.status(),
          tables: await page.locator('table').count(),
          hasErrorBoundary: /Server Components render|An error occurred|Cannot read properties/i.test(body),
          pageErrors: [...errors],
          failedResponses: [...failedResponses],
          rendered: body.trim().length > 300,
        };
        pages.push(report);
        console.log(JSON.stringify({ user: username, ...report }));
        assert.ok(navigation.ok(), `${username}: failed page ${report.page}`);
        assert.ok(report.rendered, `${username}: page did not render ${report.page}`);
        assert.ok(!report.hasErrorBoundary, `${username}: error boundary ${report.page}`);
        assert.equal(report.pageErrors.length, 0, `${username}: browser exceptions ${report.page}`);
        assert.equal(report.failedResponses.length, 0, `${username}: API failures ${report.page}`);
        if (route === '/incidents') {
          assert.equal(await page.getByRole('button', { name: 'Create Incident', exact: true }).isDisabled(), permissions.role !== 'admin', `${username}: create incident permission`);
        }
        if (route === '/alerts/feed' && permissions.role === 'viewer') {
          await page.locator('table tbody tr').first().getByTestId('dropdown-menu-button').click();
          const menu = page.getByTestId('dropdown-menu-list');
          await menu.waitFor({ state: 'visible' });
          for (const name of ['Enrich', 'Change Status', 'Self-Assign', 'Run Workflow']) {
            const action = menu.locator('button').filter({ hasText: name });
            assert.ok(await action.isDisabled(), `${username}: ${name} must be disabled in RO`);
            assert.match(await action.getAttribute('title'), /Read only/, `${username}: ${name} needs an RO explanation`);
          }
          assert.ok(await menu.locator('button').filter({ hasText: 'View Alert' }).isEnabled(), `${username}: reading alert payload must remain allowed`);
          await page.keyboard.press('Escape');
        }
        if (route.startsWith('/incidents/')) {
          const id = decodeURIComponent(route.split('/')[2]);
          const incident = incidents.items.find(item => item.id === id);
          const editable = permissions.role === 'admin' || permissions.writable_teams.includes(incident.team_id);
          assert.equal(await page.getByRole('button', { name: 'Edit Incident', exact: true }).isDisabled(), !editable, `${username}: incident edit permission`);
          const statusInput = page.getByRole('combobox', { name: 'Incident status', exact: true });
          assert.equal(await statusInput.isDisabled(), !editable, `${username}: incident status permission`);
          if (!editable) assert.ok(body.includes('Read only:'), `${username}: missing RO explanation`);
        }
        if (username === 'it-admin' && route === '/incidents') await page.screenshot({ path: path.join(tempDir, 'incidents.png') });
      }
      errors = [];
      failedResponses = [];
      await checkViews(page, context, username);
      assert.equal(errors.length, 0, `${username}: browser exceptions in views`);
      assert.equal(failedResponses.length, 0, `${username}: API failures in views`);
      for (const endpoint of ['/alerts/facets', '/incidents/facets']) {
        currentStep = `${username}: ${endpoint}`;
        const response = await context.request.get(`${origin}/v2${endpoint}`);
        assert.equal(response.status(), 200, `${username}: ${endpoint}`);
        assert.ok(Array.isArray(await response.json()), `${username}: invalid facets payload`);
      }
      currentStep = `${username}: logout`;
      await page.getByRole('button').filter({ hasText: session.user.name || session.user.email }).click();
      await page.getByRole('menuitem', { name: 'Sign out', exact: true }).click();
      await page.locator('#username').waitFor({ state: 'visible', timeout: 20000 });
      const remainingSessions = (await context.cookies()).filter(cookie =>
        /^_oauth2_proxy(?:_\d+)?$/.test(cookie.name) ||
        /^(?:__Secure-)?(?:authjs|next-auth)\.session-token(?:\.\d+)?$/.test(cookie.name)
      );
      assert.equal(remainingSessions.length, 0, `${username}: session cookies remain after sign out`);
      const authResponse = await context.request.get(`${origin}/oauth2/auth`, { maxRedirects: 0 });
      assert.equal(authResponse.status(), 401, `${username}: proxy still authenticates after sign out`);
      console.log(JSON.stringify({ user: username, logout: 'ok', loginForm: true, remainingSessions: 0 }));
      if (username === 'it-admin') {
        const switchedUser = realm.users.find(value => value.username === 'ops-l1');
        await page.locator('#username').fill(switchedUser.username);
        await page.locator('#password').fill(switchedUser.credentials.find(value => value.type === 'password').value);
        await Promise.all([
          page.waitForNavigation({ waitUntil: 'domcontentloaded', timeout: 20000 }),
          page.locator('#kc-login').click(),
        ]);
        await page.waitForTimeout(3500);
        const switchedSession = await (await context.request.get(`${origin}/api/auth/session`)).json();
        assert.equal(switchedSession.user?.email, switchedUser.email, 'account switch kept the previous identity');
        const switchedIncidentsResponse = await context.request.get(`${origin}/v2/incidents?limit=100`);
        assert.equal(switchedIncidentsResponse.status(), 200, 'account switch: incidents API');
        const switchedIncidents = await switchedIncidentsResponse.json();
        const switchedPermissions = await checkPermissions(context, 'ops-l1');
        await checkTeamVisibilityAndWriteDenial(context, 'ops-l1', switchedIncidents, switchedPermissions, fixtures, adminContext);
        console.log(JSON.stringify({ accountSwitch: 'it-admin -> ops-l1', result: 'ok' }));
        await page.getByRole('button').filter({ hasText: switchedSession.user.name || switchedSession.user.email }).click();
        await page.getByRole('menuitem', { name: 'Sign out', exact: true }).click();
        await page.locator('#username').waitFor({ state: 'visible', timeout: 20000 });
      }
      results.push({ user: username, pages: pages.length, facets: 'ok', logout: 'ok' });
      await context.close();
    }
    console.log(JSON.stringify({ result: 'passed', checks: results }));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(JSON.stringify({ step: currentStep, error: safe(error.message) })); process.exitCode = 1; });
