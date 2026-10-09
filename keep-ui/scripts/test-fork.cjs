const fs = require('node:fs');
const path = require('node:path');
const { execFileSync, spawnSync } = require('node:child_process');

const ui = path.resolve(__dirname, '..');
const root = path.resolve(ui, '..');
const DEFAULT_FORK_TESTS = [
  '__tests__/notification-login.test.ts',
  'app/(keep)/incidents/[id]/__tests__/route.test.ts',
  'app/(keep)/incidents/[id]/enrichments/__tests__/EnrichmentEditableField.test.tsx',
  'app/(keep)/settings/__tests__/settings-teams.test.tsx',
  'app/(keep)/settings/auth/__tests__/teams-tab.test.tsx',
  'app/(keep)/silences/__tests__/silences-registry.test.tsx',
  'app/(signin)/signin/__tests__/oauth2proxy-redirect.test.tsx',
  'app/api/copilotkit/__tests__/route.test.ts',
  'entities/incidents/lib/__tests__/incident-views.test.ts',
  'entities/silences/model/__tests__/useSilences.test.ts',
  'features/filter/__tests__/facets-panel.test.tsx',
  'features/filter/store/__tests__/filter-persistence.test.ts',
  'features/filter/store/__tests__/use-initial-state-handler.test.ts',
  'features/filter/store/use-query-params/__tests__/use-query-params.test.ts',
  'features/incidents/change-incident-status/ui/__tests__/incident-change-status-select.test.tsx',
  'features/incidents/create-or-update-incident/ui/__tests__/create-or-update-incident-form.test.tsx',
  'features/incidents/notification-command/ui/__tests__/NotificationCommand.test.tsx',
  'features/presets/create-or-update-preset/ui/__tests__/create-or-update-preset-form.test.tsx',
  'features/presets/custom-preset-links/ui/__tests__/presets-noise.test.tsx',
  'features/presets/presets-manager/ui/__tests__/preset-navigation.test.ts',
  'features/silences/__tests__/SilenceBadge.test.tsx',
  'features/silences/__tests__/SilenceModal.test.tsx',
  'features/silences/__tests__/UnsilenceModal.test.tsx',
  'shared/api/__tests__/ApiClient.test.ts',
  'shared/api/server/__tests__/createServerApiClient.test.ts',
  'shared/lib/__tests__/logs-utils.test.ts',
  'shared/lib/__tests__/provider-utils.test.ts',
  'shared/lib/hooks/__tests__/useSignOut.test.ts',
  'shared/lib/hooks/__tests__/useUserPermissions.test.ts',
  'shared/ui/CorrelationExplanation/__tests__/CorrelationExplanation.test.tsx',
  'shared/ui/DropdownMenu/__tests__/dropdown-menu.test.tsx',
  'shared/ui/EventPresentation/__tests__/EventPresentation.test.tsx',
  'shared/ui/TablePagination/__tests__/TablePagination.test.tsx',
  'shared/ui/__tests__/DateTimeField.test.tsx',
  'widgets/workflow-builder/__tests__/workflow-builder-widget.test.tsx',
  'widgets/workflow-builder/__tests__/workflow-builder.test.tsx',
];

function resolveTests() {
  const upstreamFile = path.join(root, 'FORK_UPSTREAM');
  let changed = '';
  if (fs.existsSync(upstreamFile)) {
    const upstream = fs.readFileSync(upstreamFile, 'utf8').trim();
    if (upstream) {
      try {
        execFileSync('git', ['cat-file', '-e', `${upstream}^{commit}`], { cwd: root, stdio: 'ignore' });
        changed = execFileSync('git', ['diff', '--name-only', upstream, '--', 'keep-ui'],
          { cwd: root, encoding: 'utf8' });
      } catch {
        // upstream commit object is absent in shallow clone, orphan branch or standalone repository
      }
    }
  }

  let untracked = '';
  try {
    untracked = execFileSync('git', ['ls-files', '--others', '--exclude-standard', 'keep-ui'],
      { cwd: root, encoding: 'utf8' });
  } catch {
    // git not available or detached
  }

  const detected = [...new Set((changed + '\n' + untracked).split('\n'))]
    .filter(name => /\.(test|spec)\.tsx?$/.test(name) && fs.existsSync(path.join(root, name)))
    .map(name => name.replace(/^keep-ui\//, ''));

  const candidates = detected.length > 0 ? detected : DEFAULT_FORK_TESTS;
  return [...new Set(candidates)]
    .filter(name => fs.existsSync(path.join(ui, name)))
    .sort();
}

const tests = resolveTests();
if (!tests.length) throw new Error('No fork tests found; check Git history and FORK_UPSTREAM');
const temp = process.env.TMPDIR || path.join(root, '.lab-work/ui-tests/tmp');
fs.mkdirSync(temp, { recursive: true });
const result = spawnSync(process.execPath, [path.join(ui, 'node_modules/jest/bin/jest.js'),
  '--runTestsByPath', ...tests, '--runInBand', ...process.argv.slice(2)], {
  cwd: ui, stdio: 'inherit',
  env: { ...process.env, TMPDIR: temp, TMP: temp, TEMP: temp },
});
if (result.error) throw result.error;
process.exit(result.status === null ? 1 : result.status);
