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
