import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { parse } from 'yaml';

const root = fileURLToPath(new URL('../', import.meta.url));
const workflow = (name) => parse(readFileSync(resolve(root, '.github/workflows', name), 'utf8'));
const defaultBranch = '${{ github.event.repository.default_branch }}';

for (const [file, mode, permission] of [
  ['review-gate.yml', 'review', { contents: 'read', 'pull-requests': 'read', statuses: 'write' }],
  ['automerge.yml', 'merge', { contents: 'write', 'pull-requests': 'write', checks: 'read', statuses: 'read' }],
]) {
  test(`${file}: write token only runs trusted default-branch code`, () => {
    const w = workflow(file);
    assert.deepEqual(w.permissions, permission);
    assert.equal(w.env, undefined);
    assert.equal(w.defaults, undefined);
    assert.equal(w.concurrency['cancel-in-progress'], false);
    for (const job of Object.values(w.jobs)) {
      assert.equal(job.permissions, undefined);
      assert.equal(job.env, undefined);
      assert.equal(job.defaults, undefined);
      assert.equal(job.container, undefined);
      assert.equal(job.services, undefined);
      assert.equal(job.environment, undefined);
      assert.equal(job['runs-on'], 'ubuntu-24.04');
      assert.ok(job['timeout-minutes'] <= 10);
      const [checkout, execute, ...unexpected] = job.steps;
      assert.deepEqual(unexpected, []); // A write-token job has this explicit execution allowlist.
      assert.match(checkout.uses, /^actions\/checkout@[a-f0-9]{40}$/);
      assert.deepEqual(checkout.with, { ref: defaultBranch, 'persist-credentials': false });
      assert.equal(execute.run, `python3 scripts/github_pr_automation.py ${mode} --apply`);
      assert.equal(execute.env.GH_TOKEN, '${{ github.token }}');
    }
  });

  test(`${file}: no PR-head or caller-selected workflow trigger`, () => {
    const triggers = workflow(file).on;
    assert.equal(triggers.pull_request, undefined);
    assert.equal(triggers.pull_request_review, undefined);
    assert.equal(triggers.workflow_dispatch, undefined);
    assert.equal(triggers.repository_dispatch, undefined);
    assert.deepEqual(triggers.workflow_run.types, ['completed']);
    assert.ok(triggers.workflow_run.workflows.includes('CI'));
    assert.ok(triggers.schedule.length > 0);
  });
}

test('review gate observes lifecycle changes without executing their head', () => {
  const w = workflow('review-gate.yml');
  assert.deepEqual([...w.on.pull_request_target.types].sort(), [
    'opened', 'reopened', 'synchronize', 'ready_for_review', 'converted_to_draft', 'closed',
  ].sort());
  assert.equal(w.jobs['publish-review'].steps[1].env.EVENT_HEAD, '${{ github.event.pull_request.head.sha }}');
  assert.equal(w.on.schedule[0].cron, '*/5 * * * *');
});

test('merger only wakes from trusted workflow completion or its sweep', () => {
  const w = workflow('automerge.yml');
  assert.deepEqual(Object.keys(w.on).sort(), ['schedule', 'workflow_run']);
  assert.ok(w.on.workflow_run.workflows.includes('review-gate'));
});

test('validation CI stays read-only and checks policy alongside OpenSpec', () => {
  const w = workflow('ci.yml');
  assert.deepEqual(w.permissions, { contents: 'read' });
  assert.equal(w.jobs.planning.name, 'planning');
  assert.equal(w.jobs['public-content'].name, 'public-content');
  for (const job of Object.values(w.jobs)) assert.equal(job.permissions, undefined);
  const commands = w.jobs.planning.steps.map((s) => s.run).filter(Boolean);
  assert.ok(commands.includes('npm run check:policy'));
  assert.ok(commands.includes('npm run check:spec'));
  assert.ok(commands.some((c) => c.startsWith('npm ci --ignore-scripts')));
  const checkout = w.jobs.planning.steps.find((step) => step.uses?.startsWith('actions/checkout@'));
  assert.equal(checkout.with['fetch-depth'], 0); // changelog release checks need tags
});

// The container image job is a build proof, not a publication path. These
// checks read the effective YAML, so quoting, bracket access and defaults are
// all resolved before they are judged; the Python suite keeps only the narrow
// text shape of the committed job.
const ciImageJob = 'container-image';
const ciImageCommand = 'docker build --tag ci-burst:ci .';
const credentialReference =
  /\bsecrets\s*(?:\.\s*[A-Za-z_][A-Za-z0-9_]*|\[)|\bgithub\s*(?:\.\s*token\b|\[\s*['"]token['"]\s*\])/i;
const publishOrLogin =
  /(?:^|\/)(?:login-action|build-push-action)(?:@|$)|\b(?:docker|podman|buildah)\b[\s\S]*\b(?:push|login)\b|\s--push\b/i;

function allStrings(value) {
  if (typeof value === 'string') return [value];
  if (Array.isArray(value)) return value.flatMap(allStrings);
  if (value && typeof value === 'object') {
    return Object.entries(value).flatMap(([key, item]) => [key, ...allStrings(item)]);
  }
  return [];
}

function ciImageViolations(w) {
  const violations = [];
  const job = w.jobs?.[ciImageJob];
  if (!job) return [`jobs.${ciImageJob} is missing`];

  for (const value of allStrings(w)) {
    if (credentialReference.test(value)) violations.push(`credential reference: ${value}`);
    if (publishOrLogin.test(value)) violations.push(`publish or login operation: ${value}`);
  }

  const permissions = w.permissions ?? {};
  if (permissions.contents !== 'read' || Object.keys(permissions).length !== 1) {
    violations.push(`workflow permissions are not exactly contents: read: ${JSON.stringify(permissions)}`);
  }
  for (const [name, other] of Object.entries(w.jobs)) {
    if (other.permissions !== undefined) violations.push(`job ${name} overrides permissions`);
  }

  if (w.defaults?.run?.['working-directory'] !== undefined) {
    violations.push(`workflow defaults set working-directory: ${w.defaults.run['working-directory']}`);
  }
  if (job.defaults?.run?.['working-directory'] !== undefined) {
    violations.push(`job defaults set working-directory: ${job.defaults.run['working-directory']}`);
  }
  for (const [index, step] of (job.steps ?? []).entries()) {
    if (step['working-directory'] !== undefined) {
      violations.push(`step ${index + 1} sets working-directory: ${step['working-directory']}`);
    }
  }

  const buildStep = (job.steps ?? []).find((step) => step.run === ciImageCommand);
  if (!buildStep) violations.push(`the image job must run exactly: ${ciImageCommand}`);

  const checkout = (job.steps ?? []).find((step) => step.uses?.startsWith('actions/checkout@'));
  if (checkout?.with?.path !== undefined && checkout.with.path !== '.') {
    violations.push(`checkout path moves the repository root: ${checkout.with.path}`);
  }

  const buildRoot =
    buildStep?.['working-directory'] ??
    job.defaults?.run?.['working-directory'] ??
    w.defaults?.run?.['working-directory'] ??
    '.';
  if (buildRoot !== '.') violations.push(`effective build working directory is ${buildRoot}`);
  return violations;
}

test('container image CI job is credential-free and builds at the repository root', () => {
  assert.deepEqual(ciImageViolations(workflow('ci.yml')), []);
});

test('container image CI checker rejects credential and working-directory spellings', () => {
  const source = readFileSync(resolve(root, '.github/workflows/ci.yml'), 'utf8');
  const jobHeader = '    name: container-image builds\n';
  const withJobEnv = (entry) => source.replace(jobHeader, `    env:\n      ${entry}\n${jobHeader}`);
  const mutations = [
    [
      'dot-access secret in the job env',
      withJobEnv('CI_TEST_TOKEN: ${{ secrets.CI_TEST_TOKEN }}'),
      /credential reference/,
    ],
    [
      'bracket-access secret in the job env',
      withJobEnv("CI_TEST_TOKEN: ${{ secrets['CI_TEST_TOKEN'] }}"),
      /credential reference/,
    ],
    [
      'double-quoted bracket secret in the job env',
      withJobEnv('CI_TEST_TOKEN: ${{ secrets["CI_TEST_TOKEN"] }}'),
      /credential reference/,
    ],
    [
      'bracketed github token in the job env',
      withJobEnv("CI_TEST_TOKEN: ${{ github['token'] }}"),
      /credential reference/,
    ],
    [
      'quoted working-directory on the build step',
      source.replace(
        `        run: ${ciImageCommand}`,
        `        "working-directory": ci_burst\n        run: ${ciImageCommand}`,
      ),
      /working-directory/,
    ],
    [
      'quoted workflow defaults working-directory',
      source.replace(
        'jobs:\n',
        'defaults:\n  run:\n    "working-directory": elsewhere\njobs:\n',
      ),
      /working-directory/,
    ],
    [
      'job defaults working-directory',
      source.replace(
        '    timeout-minutes: 10\n',
        '    timeout-minutes: 10\n    defaults:\n      run:\n        working-directory: elsewhere\n',
      ),
      /working-directory/,
    ],
    [
      'workflow-level package permission',
      source.replace(
        'permissions:\n  contents: read',
        'permissions:\n  contents: read\n  packages: write',
      ),
      /permissions/,
    ],
    [
      'job-level permission',
      source.replace(
        '    timeout-minutes: 10\n',
        '    timeout-minutes: 10\n    permissions:\n      contents: write\n',
      ),
      /permissions/,
    ],
    [
      'registry login action',
      source.replace(
        '      - name: Build the image from the repository root\n',
        '      - uses: docker/login-action@v3\n      - name: Build the image from the repository root\n',
      ),
      /publish or login/,
    ],
    [
      'registry push step',
      source.replace(
        '      - name: Build the image from the repository root\n',
        '      - name: Publish the image\n        run: docker image push ci-burst:ci\n'
          + '      - name: Build the image from the repository root\n',
      ),
      /publish or login/,
    ],
  ];
  for (const [label, mutated, reason] of mutations) {
    assert.notEqual(mutated, source, `${label}: the mutation must apply`);
    const violations = ciImageViolations(parse(mutated));
    assert.ok(
      violations.some((violation) => reason.test(violation)),
      `${label}: expected ${reason}, got ${JSON.stringify(violations)}`,
    );
  }
});
