"""Execute the actual github-script bodies with a mocked GitHub API.

Run with: uv run --with pyyaml python -m unittest discover -s tests
"""
import json
from pathlib import Path
import subprocess
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
HARNESS = r'''
const fs = require('node:fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
Object.assign(process.env, input.env);
const result = { outputs: {}, calls: [], warnings: [], failed: null };
const core = {
  setOutput: (key, value) => { result.outputs[key] = value; },
  setFailed: message => { result.failed = message; },
  warning: message => result.warnings.push(message),
  summary: {
    addHeading() { return this; }, addLink() { return this; }, async write() {},
  },
};
const github = {
  rest: {
    checks: {
      listForRef: 'checks',
      create: async params => {
        result.calls.push({ api: 'create', params });
        if (input.fail) throw new Error('API unavailable');
      },
    },
    actions: { getWorkflowRun: async params => {
      result.calls.push({ api: 'source', params });
      return { data: input.source };
    } },
  },
  paginate: async (api, params) => {
    result.calls.push({ api, params });
    if (input.fail) throw new Error('API unavailable');
    return input.checks || [];
  },
};
const AsyncFunction = Object.getPrototypeOf(async function() {}).constructor;
new AsyncFunction('github', 'context', 'core', 'require', input.script)(github, input.context, core, require)
  .then(() => process.stdout.write(JSON.stringify(result)))
  .catch(error => { console.error(error); process.exitCode = 1; });
'''


class ResultTests(unittest.TestCase):
    def execute(self, mode, *, env=None, context=None, **data):
        workflow = yaml.load((ROOT / '.github/workflows' / f'result-{mode}.yaml').read_text(), Loader=yaml.BaseLoader)
        script = workflow['jobs'][mode]['steps'][0]['with']['script']
        request = {
            'script': script,
            'env': {
                'RESULT_TASK': 'unit-tests', 'RESULT_IDENTITY': 'v1:build-id',
                'RESULT_ENABLED': 'true', 'RESULT_SUCCESSFUL': 'true',
                'RESULT_REUSED': 'false', 'RESULT_OUTPUTS': '{"build_id":"example"}',
                'GITHUB_RUN_ATTEMPT': '1',
                'GITHUB_WORKFLOW_REF': 'example/app/.github/workflows/ci.yaml@refs/heads/main',
                **(env or {}),
            },
            'context': {
                'repo': {'owner': 'example', 'repo': 'app'}, 'sha': 'abc',
                'runId': 2, 'eventName': 'push', 'payload': {},
                'serverUrl': 'https://github.com', **(context or {}),
            },
            'source': {'head_sha': 'abc', 'path': '.github/workflows/ci.yaml', 'html_url': 'https://github.com/example/app/actions/runs/1'},
            **data,
        }
        process = subprocess.run(['node', '-e', HARNESS], input=json.dumps(request), text=True, capture_output=True, check=True)
        return json.loads(process.stdout)

    def check(self):
        recorded = self.execute('record', context={'runId': 1})['calls'][0]['params']
        return {**recorded, 'app': {'slug': 'github-actions'}}

    def test_round_trip_restores_structured_outputs(self):
        result = self.execute('lookup', checks=[self.check()])
        self.assertEqual(result['outputs']['hit'], 'true')
        self.assertEqual(json.loads(result['outputs']['outputs']), {'build_id': 'example'})
        self.assertIn('/runs/1', result['outputs']['source_url'])
        self.assertEqual(result['calls'][0]['params']['filter'], 'all')
        self.assertEqual(result['calls'][0]['params']['ref'], 'abc')

    def test_rejects_invalid_or_untrusted_records(self):
        for changes in [
            {'conclusion': 'failure'}, {'conclusion': 'skipped'},
            {'external_id': 'wrong-key'}, {'head_sha': 'another-commit'},
            {'app': {'slug': 'another-app'}}, {'output': {'text': 'not json'}},
        ]:
            with self.subTest(changes=changes):
                result = self.execute('lookup', checks=[{**self.check(), **changes}])
                self.assertEqual(result['outputs']['hit'], 'false')
        for changes in [
            {'schema': 2}, {'task': 'lint'}, {'identity': 'different-build'},
            {'commit': 'different-commit'}, {'workflow': 'another/workflow'},
            {'source_run_id': 2}, {'outputs': []}, {'outputs': None},
        ]:
            with self.subTest(record=changes):
                check = self.check()
                payload = json.loads(check['output']['text']) | changes
                check['output']['text'] = json.dumps(payload)
                self.assertEqual(self.execute('lookup', checks=[check])['outputs']['hit'], 'false')

    def test_verifies_source_workflow_and_commit(self):
        for source in [
            {'head_sha': 'other', 'path': '.github/workflows/ci.yaml'},
            {'head_sha': 'abc', 'path': '.github/workflows/other.yaml'},
        ]:
            self.assertEqual(self.execute('lookup', checks=[self.check()], source=source)['outputs']['hit'], 'false')

    def test_cache_miss_and_api_failure_run_work(self):
        self.assertEqual(self.execute('lookup')['outputs']['hit'], 'false')
        result = self.execute('lookup', fail=True)
        self.assertEqual(result['outputs']['hit'], 'false')
        self.assertEqual(len(result['warnings']), 1)

    def test_manual_runs_reruns_and_disabled_lookup_bypass_reuse(self):
        for options in [
            {'env': {'GITHUB_RUN_ATTEMPT': '2'}},
            {'env': {'RESULT_ENABLED': 'false'}},
            {'context': {'eventName': 'workflow_dispatch'}},
        ]:
            result = self.execute('lookup', checks=[self.check()], **options)
            self.assertEqual(result['outputs']['hit'], 'false')
            self.assertEqual(result['calls'], [])

    def test_failed_work_fails_gate_without_recording(self):
        result = self.execute('record', env={'RESULT_SUCCESSFUL': 'false'})
        self.assertIsNotNone(result['failed'])
        self.assertEqual(result['calls'], [])

    def test_reuse_passes_gate_without_creating_transitive_records(self):
        result = self.execute('record', env={'RESULT_SUCCESSFUL': 'false', 'RESULT_REUSED': 'true'})
        self.assertIsNone(result['failed'])
        self.assertEqual(result['calls'], [])

    def test_recording_failure_does_not_fail_successful_work(self):
        result = self.execute('record', fail=True)
        self.assertIsNone(result['failed'])
        self.assertEqual(len(result['warnings']), 1)

    def test_forks_do_not_publish_reusable_results(self):
        result = self.execute('record', context={'payload': {'pull_request': {'head': {'repo': {'full_name': 'outsider/app'}}}}})
        self.assertEqual(result['calls'], [])

    def test_invalid_output_is_not_published(self):
        for value in ['[]', 'null', 'invalid']:
            result = self.execute('record', env={'RESULT_OUTPUTS': value})
            self.assertEqual(result['calls'], [])
            self.assertEqual(len(result['warnings']), 1)

    def build_script(self, job, step_id):
        workflow = yaml.load((ROOT / '.github/workflows/build-cached.yaml').read_text(), Loader=yaml.BaseLoader)
        return next(step['with']['script'] for step in workflow['jobs'][job]['steps'] if step.get('id') == step_id)

    def test_build_metadata_requires_the_expected_image_commit_and_digest(self):
        valid = {
            'image': 'registry/app', 'image_tag': 'git-abcdef0',
            'image_digest': 'sha256:' + 'a' * 64, 'build_id': 'build-1',
        }
        for changes, expected in [
            ({}, 'true'), ({'image': 'registry/other'}, 'false'),
            ({'image_tag': 'git-0000000'}, 'false'),
            ({'image_digest': ''}, 'false'), ({'build_id': ''}, 'false'),
        ]:
            with self.subTest(changes=changes):
                result = self.execute('lookup', script=self.build_script('prepare', 'validate'),
                                      context={'sha': 'abcdef0123456789'},
                                      env={'CACHED_OUTPUTS': json.dumps(valid | changes), 'EXPECTED_IMAGE': 'registry/app'})
                self.assertEqual(result['outputs']['valid'], expected)

    def test_deployment_context_comes_from_current_ref(self):
        for ref, expected in [('refs/heads/main', ''), ('refs/heads/deployment/fi/production', 'fi')]:
            result = self.execute('lookup', script=self.build_script('prepare', 'context'), context={'ref': ref})
            self.assertEqual(result['outputs']['deployment_env'], expected)

    def test_restored_outputs_do_not_restore_deployment_context(self):
        result = self.execute('lookup', script=self.build_script('finish', 'outputs'), env={
            'BUILD_OUTPUTS': json.dumps({'image': 'registry/app', 'build_id': 'build-1', 'deployment_env': 'old-env'}),
        })
        self.assertEqual(result['outputs']['image'], 'registry/app')
        self.assertNotIn('deployment_env', result['outputs'])


if __name__ == '__main__':
    unittest.main()
