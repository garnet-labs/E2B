import unittest
import base64
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, call, patch

import yaml

from mirror import API, FORK, INSTALL_PATTERNS, RECORDER, SOURCE, VERDICT, WORKFLOW, BaselineUnavailable, Mirror, dependency_file, path_matches, recorder_installs, scrub_title, version_transitions


class RecorderPermissionsTest(unittest.TestCase):
    def test_recorder_jobs_grant_oidc_for_shadow_pull_requests_and_bases(self):
        path = Path(__file__).resolve().parents[2] / 'workflows' / RECORDER
        workflow = yaml.load(path.read_text(encoding='utf-8'), Loader=yaml.BaseLoader)
        self.assertEqual(workflow['permissions'], {})
        self.assertIn('pull_request', workflow['on'])
        self.assertIn('workflow_dispatch', workflow['on'])
        self.assertIn('shadow/base/**', workflow['on']['push']['branches'])
        self.assertEqual(set(workflow['jobs']), {'install-test', 'docker-build'})
        for name, job in workflow['jobs'].items():
            with self.subTest(job=name):
                self.assertNotIn('if', job)
                self.assertEqual(job['permissions'], {'contents': 'read', 'id-token': 'write'})


class ReviewerWorkflowTest(unittest.TestCase):
    def test_shadow_reviews_do_not_mirror_the_description(self):
        path = Path(__file__).resolve().parents[2] / 'workflows' / 'garnet-evidence-mirror.yml'
        workflow = yaml.load(path.read_text(encoding='utf-8'), Loader=yaml.BaseLoader)
        job = workflow['jobs']['mirror']
        steps = {step.get('name'): step for step in job['steps']}
        mirror = steps['Mirror into every open pull request at this head']
        rereview = steps['Request reviews once the record is bound (once per head)']
        self.assertIn("&& steps.head.outputs.shadow_pr != 'true'", mirror['if'])
        self.assertIn("steps.head.outputs.shadow_pr != 'true' || github.repository == 'garnet-labs/E2B'", rereview['if'])
        self.assertEqual(rereview['env']['SHADOW_PR'], '${{ steps.head.outputs.shadow_pr }}')
        self.assertIn("'devin,greptile'", job['env']['GARNET_REVIEWERS'])
        guard = steps['Report skipped shadow re-review']
        self.assertIn("steps.head.outputs.shadow_pr == 'true'", guard['if'])
        for step in (mirror, rereview):
            self.assertIn(f"steps.{step['id']}.outcome == 'skipped'", guard['if'])
        self.assertIn('::error::', guard['run'])
        self.assertIn('exit 1', guard['run'])


class PathsTest(unittest.TestCase):
    def test_recursive_and_direct(self):
        patterns = ['package.json', 'products/desktop/**/package.json']
        for path in ['package.json', 'products/desktop/package.json', 'products/desktop/a/b/package.json']:
            self.assertTrue(path_matches(path, patterns))
        self.assertFalse(path_matches('other/package.json', patterns))
        self.assertFalse(path_matches('a/b/package.json', ['a/*/other.json']))

    def test_empty_and_workflow(self):
        self.assertFalse(path_matches('package.json', []))
        self.assertFalse(path_matches(WORKFLOW, ['**']))

class TitleTest(unittest.TestCase):
    def test_scrub(self):
        reference = '#' + '123'
        mention = chr(64) + 'someone'
        self.assertEqual(scrub_title(f'Bump pnpm {reference} {mention} https://example.com/a {SOURCE}'), 'Bump pnpm')

    def test_empty(self):
        self.assertEqual(scrub_title(''), 'Dependency update')
        self.assertEqual(scrub_title('#' + '123 ' + chr(64) + 'bot'), 'Dependency update')

    def test_normal(self):
        self.assertEqual(scrub_title('Bump pnpm from 9 to 10'), 'Bump pnpm from 9 to 10')

    def test_package_tokens_are_preserved_and_idempotent(self):
        text = 'Update @scope/name npm:@scope/name@1.2.3 package@2.0 ' + chr(64) + 'reviewer'
        expected = 'Update `@scope/name` `npm:@scope/name@1.2.3` `package@2.0`'
        self.assertEqual(scrub_title(text), expected)
        self.assertEqual(scrub_title(expected), expected)


class MirrorDiscoveryTest(unittest.TestCase):
    def make_pr(self):
        return {'number': 1, 'body': '<!-- garnet-shadow-mirror upstream=1 upstream_head=' + 'a' * 40 + ' upstream_base=' + 'b' * 40 + ' -->',
                'head': {'repo': {'full_name': FORK}, 'ref': 'shadow/pr/1-r2'},
                'base': {'repo': {'full_name': FORK}, 'ref': 'shadow/base/1-r2'}}

    def discover(self, prs):
        api = Mock()
        workflow = b'on:\n  pull_request:\n  push:\n    branches: [main, "shadow/base/**"]\n'
        api.call.side_effect = [{'content': base64.b64encode(workflow).decode()}, {'tree': []}]
        api.pages.return_value = prs
        return Mirror(api)

    def test_valid_same_repository_pair_is_discovered(self):
        pr = self.make_pr()
        self.assertEqual(self.discover([pr]).mirrors, {1: pr})
        self.assertEqual(self.discover([]).mirrors, {})

    def test_only_the_trusted_recorder_is_copied_to_shadow_trees(self):
        mirror = self.discover([])
        mirror.api.call.assert_has_calls([call(FORK, f'contents/{WORKFLOW}?ref=main'), call(FORK, 'git/trees/main')])
        mirror.api.call.side_effect = [
            {'tree': {'sha': 'upstream-tree'}},
            {'tree': [{'path': '.github', 'sha': 'upstream-github'}]},
        ]
        mirror.api.write.side_effect = [{'sha': 'workflows'}, {'sha': 'github'}, {'sha': 'shadow-tree'}]
        self.assertEqual(mirror.tree('upstream-commit'), 'shadow-tree')
        self.assertEqual(mirror.api.write.call_args_list, [
            call('git/trees', {'tree': [{'path': RECORDER, 'mode': '100644', 'type': 'blob', 'content': mirror.workflow}]}),
            call('git/trees', {'base_tree': 'upstream-github', 'tree': [{'path': 'workflows', 'mode': '040000', 'type': 'tree', 'sha': 'workflows'}]}),
            call('git/trees', {'base_tree': 'upstream-tree', 'tree': [{'path': '.github', 'mode': '040000', 'type': 'tree', 'sha': 'github'}]}),
        ])

    def test_replay_trees_use_reviewer_adapters_from_mirror_main(self):
        api = Mock()
        adapters = [
            {'path': name, 'mode': mode, 'type': kind, 'sha': f'main-{name}'}
            for name, mode, kind in (
                ('REVIEW.md', '100644', 'blob'), ('greptile.json', '100644', 'blob'),
                ('.coderabbit.yaml', '100644', 'blob'), ('.pr_agent.toml', '100644', 'blob'),
                ('.devin', '040000', 'tree'), ('.agents', '040000', 'tree'),
            )
        ]
        responses = {
            f'contents/{WORKFLOW}?ref=main': {'content': base64.b64encode(b'name: Recorder\n').decode()},
            'git/trees/main': {'tree': adapters + [
                {'path': 'package.json', 'mode': '100644', 'type': 'blob', 'sha': 'main-package'},
            ]},
        }
        for side in ('base', 'head'):
            responses[f'git/commits/upstream-{side}'] = {'tree': {'sha': f'upstream-{side}-tree'}}
            responses[f'git/trees/upstream-{side}-tree'] = {'tree': [
                {'path': '.github', 'sha': f'upstream-{side}-github'},
                {'path': 'REVIEW.md', 'sha': f'upstream-{side}-review'},
            ]}
        api.call.side_effect = lambda repo, path: responses[path]
        api.pages.return_value = []
        api.write.side_effect = [{'sha': sha} for sha in ('workflows', 'base-github', 'base-tree',
                                                         'workflows', 'head-github', 'head-tree')]
        mirror = Mirror(api)
        self.assertEqual(mirror.tree('upstream-base'), 'base-tree')
        self.assertEqual(mirror.tree('upstream-head'), 'head-tree')
        for i, side in ((2, 'base'), (5, 'head')):
            self.assertEqual(api.write.call_args_list[i], call('git/trees', {
                'base_tree': f'upstream-{side}-tree',
                'tree': [{'path': '.github', 'mode': '040000', 'type': 'tree', 'sha': f'{side}-github'}, *adapters],
            }))

    def test_spoofed_marker_is_ignored(self):
        for side, key, value in [('head', 'repo', {'full_name': 'other/repository'}),
                                  ('base', 'repo', {'full_name': 'other/repository'}),
                                  ('head', 'repo', None), ('base', 'repo', None),
                                  ('head', 'ref', 'shadow/pr/2-r2'), ('base', 'ref', 'shadow/base/2-r2'),
                                  ('head', 'ref', 'shadow/pr/1-r2-extra'), ('base', 'ref', 'master'),
                                  ('base', 'ref', 'shadow/base/1-r3')]:
            with self.subTest(side=side, key=key, value=value):
                pr = deepcopy(self.make_pr())
                pr[side][key] = value
                self.assertEqual(self.discover([pr]).mirrors, {})


class DeferredPRTest(unittest.TestCase):
    def make_mirror(self, no_open_pr=True):
        mirror = Mirror.__new__(Mirror)
        mirror.api = Mock()
        mirror.dry_run = False
        mirror.no_open_pr = no_open_pr
        mirror.mirrors = {}
        mirror.patterns = ['package.json']
        mirror.tree = Mock(return_value='head-tree')
        mirror.commit = Mock(return_value='new-head')
        mirror.ref = Mock()
        mirror.baseline_started = Mock()
        def call(repo, path):
            if path.startswith('git/matching-refs/'):
                return []
            if path.startswith('compare/'):
                return {'merge_base_commit': {'sha': 'source-merge-base'}}
            if path.startswith('contents/pnpm-workspace.yaml'):
                return {'content': base64.b64encode(b'packages:\n  - services/mcp\n  - nodejs\n').decode()}
            if repo == SOURCE:
                return {'state': 'open', 'draft': False, 'author_association': 'CONTRIBUTOR',
                        'user': {'type': 'Bot', 'login': 'dependabot[bot]', 'id': 49699333}, 'base': {'ref': 'main', 'sha': 'source-base'},
                        'head': {'sha': 'source-head', 'repo': {'full_name': SOURCE}}, 'title': 'Bump pnpm'}
            if path.startswith('git/ref/heads/shadow/base/'):
                return {'object': {'sha': 'baseline'}}
            if path.startswith('git/ref/heads/shadow/pr/'):
                return {'object': {'sha': 'head'}}
            if path == 'git/commits/baseline':
                return {'message': 'chore: shadow baseline\n\nShadow-Upstream-Base: source-merge-base'}
            if path == 'git/commits/head':
                return {'tree': {'sha': 'head-tree'}, 'message': 'Bump pnpm', 'parents': [{'sha': 'baseline'}]}
            raise AssertionError((repo, path))
        mirror.api.call.side_effect = call
        mirror.api.pages.return_value = [{'filename': 'package.json', 'patch': '- "pnpm": "9"\n+ "pnpm": "10"'}]
        return mirror

    def test_export_reuses_existing_commits(self):
        mirror = self.make_mirror()
        with TemporaryDirectory() as directory, patch('mirror.Path', return_value=Path(directory)):
            for _ in range(2):
                result = mirror.sync(1)
            self.assertEqual(result['status'], 'ready')
            self.assertEqual(result['head'], 'head')
            self.assertEqual(Path(result['title_file']).read_text(), 'Bump pnpm\n')
            body = Path(result['body_file']).read_text()
            self.assertIn('pnpm 9 → 10', body)
            self.assertIn('Mirror base `baseline` → head `head`', body)
            self.assertIn('upstream_base=source-merge-base', body)
        mirror.commit.assert_not_called()
        mirror.ref.assert_not_called()
        mirror.api.write.assert_not_called()

    def test_failed_dispatch_allocates_fresh_pair(self):
        mirror = self.make_mirror()
        mirror.baseline_started.side_effect = [BaselineUnavailable('HTTP 403: dispatch denied'), None]
        mirror.commit.side_effect = ['fresh-baseline', 'fresh-head']
        with TemporaryDirectory() as directory, patch('mirror.Path', return_value=Path(directory)):
            result = mirror.sync(1)
        self.assertEqual(result['base_ref'], 'shadow/base/1-r2')
        self.assertEqual(result['head_ref'], 'shadow/pr/1-r2')
        self.assertEqual(mirror.baseline_started.call_args_list[1].args, ('shadow/base/1-r2', 'fresh-baseline'))
        self.assertEqual(mirror.commit.call_args_list[1].args, ('head-tree', ['fresh-baseline'], 'Bump pnpm'))
        self.assertEqual(mirror.ref.call_args_list[0].args, ('shadow/base/1-r2', 'fresh-baseline'))

    def test_existing_revision_is_reused_without_old_refs_or_pr(self):
        mirror = self.make_mirror()
        original_call = mirror.api.call.side_effect
        def call(repo, path):
            if path.startswith('git/matching-refs/'):
                return [{'ref': 'refs/heads/shadow/base/1-r2'}, {'ref': 'refs/heads/shadow/base/10-r9'}]
            return original_call(repo, path)
        mirror.api.call.side_effect = call
        self.assertEqual(mirror.branches(1, None), ('shadow/base/1-r2', 'shadow/pr/1-r2'))

    def test_denied_dispatch_reports_baseline_unavailable(self):
        mirror = self.make_mirror()
        mirror.api.call.return_value = {'workflow_runs': []}
        mirror.api.call.side_effect = None
        mirror.api.write.side_effect = RuntimeError('HTTP 403: dispatch denied')
        with patch('mirror.time.sleep'), self.assertRaisesRegex(BaselineUnavailable, 'HTTP 403'):
            Mirror.baseline_started(mirror, 'shadow/base/1', 'baseline')

    def test_failed_fresh_baseline_does_not_push_head_or_recurse(self):
        mirror = self.make_mirror()
        mirror.baseline_started.side_effect = BaselineUnavailable('No baseline recording')
        mirror.commit.return_value = 'fresh-baseline'
        with self.assertRaises(BaselineUnavailable):
            mirror.sync(1)
        mirror.commit.assert_called_once_with('head-tree', [], 'chore: shadow baseline\n\nShadow-Upstream-Base: source-merge-base')
        mirror.ref.assert_called_once_with('shadow/base/1-r2', 'fresh-baseline')

    def test_only_allowlisted_bots_with_upstream_heads_are_selected(self):
        dependabot = {'type': 'Bot', 'login': 'dependabot[bot]', 'id': 49699333}
        devin = {'type': 'Bot', 'login': 'devin-ai-integration[bot]', 'id': 158243242}
        for association, user, head_repo, expected in [
            ('NONE', {'type': 'User'}, SOURCE, 'untrusted_author'),
            ('CONTRIBUTOR', {'type': 'User'}, SOURCE, 'untrusted_author'),
            ('OWNER', {'type': 'User'}, SOURCE, 'untrusted_author'),
            ('MEMBER', {'type': 'User'}, SOURCE, 'untrusted_author'),
            ('MEMBER', {'type': 'User'}, 'shared/e2b', 'untrusted_author'),
            ('COLLABORATOR', {'type': 'User'}, SOURCE, 'untrusted_author'),
            ('NONE', {'type': 'Bot'}, SOURCE, 'untrusted_author'),
            ('NONE', dependabot, SOURCE, 'dry-run'),
            ('NONE', dependabot, 'other/e2b', 'untrusted_author'),
            ('CONTRIBUTOR', devin, SOURCE, 'dry-run'),
        ]:
            with self.subTest(association=association, user=user, head_repo=head_repo):
                mirror = self.make_mirror()
                mirror.dry_run = True
                original_call = mirror.api.call.side_effect
                def call(repo, path):
                    result = original_call(repo, path)
                    if path == 'pulls/1':
                        result.update(author_association=association, user=user)
                        result['head']['repo']['full_name'] = head_repo
                    return result
                mirror.api.call.side_effect = call
                self.assertEqual(mirror.sync(1)['status'], expected)
                mirror.api.write.assert_not_called()
                mirror.commit.assert_not_called()

    def test_new_baseline_uses_merge_base_not_current_main(self):
        mirror = self.make_mirror()
        original_call = mirror.api.call.side_effect
        def call(repo, path):
            if path.startswith('git/ref/'):
                raise RuntimeError('HTTP 404: missing ref')
            return original_call(repo, path)
        mirror.api.call.side_effect = call
        mirror.commit.side_effect = ['new-baseline', 'new-head']
        with TemporaryDirectory() as directory, patch('mirror.Path', return_value=Path(directory)):
            mirror.sync(1)
        self.assertEqual(mirror.tree.call_args_list[0].args, ('source-merge-base',))
        mirror.api.call.assert_any_call(SOURCE, 'compare/source-base...source-head')
        self.assertEqual(mirror.commit.call_args_list[0].args, ('head-tree', [], 'chore: shadow baseline\n\nShadow-Upstream-Base: source-merge-base'))
        self.assertEqual(mirror.commit.call_args_list[1].args, ('head-tree', ['new-baseline'], 'Bump pnpm'))

    def test_existing_pr_no_open_exports_marker_without_updating_pr(self):
        mirror = self.make_mirror()
        mirror.mirrors[1] = {'state': 'open', 'base': {'sha': 'baseline', 'ref': 'shadow/base/1'}, 'head': {'ref': 'shadow/pr/1'}}
        with TemporaryDirectory() as directory, patch('mirror.Path', return_value=Path(directory)):
            result = mirror.sync(1)
            self.assertIn('upstream_base=source-merge-base', Path(result['body_file']).read_text())
        mirror.api.write.assert_not_called()

    def test_changed_merge_base_allocates_fresh_baseline_revision(self):
        mirror = self.make_mirror()
        mirror.mirrors[1] = {'state': 'open', 'base': {'sha': 'old-baseline', 'ref': 'shadow/base/1'},
                             'head': {'ref': 'shadow/pr/1'},
                             'body': '<!-- garnet-shadow-mirror upstream=1 upstream_head=' + 'a' * 40 + ' upstream_base=' + 'b' * 40 + ' -->'}
        original_call = mirror.api.call.side_effect
        def call(repo, path):
            if path.endswith('-r2'):
                raise RuntimeError('HTTP 404: missing ref')
            return original_call(repo, path)
        mirror.api.call.side_effect = call
        mirror.commit.side_effect = ['fresh-baseline', 'fresh-head']
        with TemporaryDirectory() as directory, patch('mirror.Path', return_value=Path(directory)):
            result = mirror.sync(1)
        self.assertEqual(result['base_ref'], 'shadow/base/1-r2')
        self.assertEqual(result['head_ref'], 'shadow/pr/1-r2')
        self.assertEqual(mirror.tree.call_args_list[0].args, ('source-merge-base',))
        mirror.baseline_started.assert_called_once_with('shadow/base/1-r2', 'fresh-baseline')
        self.assertEqual(mirror.commit.call_args_list[1].args, ('head-tree', ['fresh-baseline'], 'Bump pnpm'))
        mirror.ref.assert_any_call('shadow/base/1-r2', 'fresh-baseline')

    def test_closed_mirror_uses_next_revision_and_reuses_prepared_refs(self):
        mirror = self.make_mirror()
        closed = {'state': 'closed', 'base': {'ref': 'shadow/base/1'}, 'head': {'ref': 'shadow/pr/1'}}
        mirror.mirrors[1] = closed
        with TemporaryDirectory() as directory, patch('mirror.Path', return_value=Path(directory)):
            result = mirror.sync(1)
            repeated = mirror.sync(1)
        self.assertEqual(result['base_ref'], 'shadow/base/1-r2')
        self.assertEqual(result['head_ref'], 'shadow/pr/1-r2')
        self.assertEqual(result, repeated)
        mirror.commit.assert_not_called()
        mirror.ref.assert_not_called()
        closed['head']['ref'] = 'shadow/pr/1-r2'
        self.assertEqual(mirror.branches(1, closed), ('shadow/base/1-r3', 'shadow/pr/1-r3'))

    def test_rebased_source_does_not_reuse_an_old_prepared_pair(self):
        mirror = self.make_mirror()
        fixture = mirror.api.call.side_effect

        def read(repo, path):
            if path == 'git/commits/baseline':
                return {'message': 'chore: shadow baseline\n\nShadow-Upstream-Base: previous-source-base'}
            if path == 'git/ref/heads/shadow/pr/1-r2':
                raise RuntimeError('HTTP 404: missing ref')
            return fixture(repo, path)

        mirror.api.call.side_effect = read
        mirror.commit.side_effect = ['rebased-baseline', 'rebased-head']
        with TemporaryDirectory() as directory, patch('mirror.Path', return_value=Path(directory)):
            result = mirror.sync(1)
            body = Path(result['body_file']).read_text()
        self.assertEqual(result['base_ref'], 'shadow/base/1-r2')
        self.assertEqual(result['head_ref'], 'shadow/pr/1-r2')
        self.assertIn('upstream_base=source-merge-base', body)
        self.assertIn('Mirror base `rebased-baseline` → head `rebased-head`', body)
        self.assertEqual(mirror.ref.call_args_list, [
            call('shadow/base/1-r2', 'rebased-baseline'), call('shadow/pr/1-r2', 'rebased-head')
        ])

    def test_base_force_update_is_always_rejected(self):
        mirror = self.make_mirror()
        for state in ('open', 'closed'):
            mirror.mirrors[1] = {'state': state}
            with self.assertRaises(ValueError):
                Mirror.ref(mirror, 'shadow/base/1', 'new-base', force=True)
        mirror.api.write.assert_not_called()

    def test_reopened_upstream_creates_new_pair_after_closed_revision(self):
        mirror = self.make_mirror()
        mirror.mirrors[1] = {'state': 'closed', 'base': {'ref': 'shadow/base/1-r2'}, 'head': {'ref': 'shadow/pr/1-r2'}}
        original_call = mirror.api.call.side_effect
        def call(repo, path):
            if path.endswith('-r3'):
                raise RuntimeError('HTTP 404: missing ref')
            return original_call(repo, path)
        mirror.api.call.side_effect = call
        mirror.commit.side_effect = ['reopened-base', 'reopened-head']
        with TemporaryDirectory() as directory, patch('mirror.Path', return_value=Path(directory)):
            result = mirror.sync(1)
        self.assertEqual(result['base_ref'], 'shadow/base/1-r3')
        self.assertEqual(result['head_ref'], 'shadow/pr/1-r3')
        mirror.ref.assert_any_call('shadow/base/1-r3', 'reopened-base')
        mirror.ref.assert_any_call('shadow/pr/1-r3', 'reopened-head')
        mirror.api.write.assert_not_called()

    def test_missing_pr_is_opened_without_new_commit(self):
        mirror = self.make_mirror(no_open_pr=False)
        mirror.api.pages.side_effect = [[{'filename': 'package.json'}], []]
        mirror.api.write.return_value = {'number': 2, 'html_url': 'https://example.com/mirror'}
        result = mirror.sync(1)
        self.assertEqual(result['status'], 'created')
        open_pr, hold = mirror.api.write.call_args_list
        self.assertEqual(open_pr.args[0], 'pulls')
        self.assertEqual(open_pr.args[1]['head'], 'shadow/pr/1')
        self.assertEqual(hold, call('issues/2/comments', {'body': VERDICT}))
        mirror.commit.assert_not_called()

    def test_unchanged_mirror_recovers_missing_comment_without_duplicate(self):
        mirror = self.make_mirror(no_open_pr=False)
        mirror.mirrors[1] = {'state': 'open', 'number': 2, 'html_url': 'https://example.com/mirror',
                             'body': '<!-- garnet-shadow-mirror upstream=1 upstream_head=abc upstream_base=def -->',
                             'base': {'sha': 'baseline', 'ref': 'shadow/base/1'}, 'head': {'ref': 'shadow/pr/1'}}
        initial_call = mirror.api.call.side_effect

        def read(repo, path):
            if path.startswith('compare/'):
                return {'merge_base_commit': {'sha': 'def'}}
            pr = initial_call(repo, path)
            pr['head']['sha'] = 'abc'
            return pr

        mirror.api.call.side_effect = read
        mirror.api.pages.side_effect = [[], [{'body': VERDICT}]]
        self.assertEqual(mirror.sync(1)['status'], 'unchanged')
        self.assertEqual(mirror.sync(1)['status'], 'unchanged')
        self.assertEqual(mirror.api.pages.call_args_list, [call(FORK, 'issues/2/comments')] * 2)
        mirror.api.write.assert_called_once_with('issues/2/comments', {'body': VERDICT})
        mirror.ref.assert_not_called()

    def test_api_rejects_upstream_writes(self):
        with patch.dict('mirror.os.environ', {'SHADOW_MIRROR_TOKEN': 'fixture'}):
            api = API()
        with patch('mirror.urllib.request.urlopen') as urlopen:
            with self.assertRaisesRegex(ValueError, 'Writes are restricted'):
                api.call(SOURCE, 'issues/1/comments', 'POST', {'body': VERDICT})
            urlopen.assert_not_called()

    def test_changed_tree_appends_commit(self):
        mirror = self.make_mirror()
        mirror.tree.return_value = 'changed-tree'
        with TemporaryDirectory() as directory, patch('mirror.Path', return_value=Path(directory)):
            result = mirror.sync(1)
        self.assertEqual(result['head'], 'new-head')
        mirror.commit.assert_called_once_with('changed-tree', ['head'], 'Bump pnpm')
        mirror.ref.assert_called_once_with('shadow/pr/1', 'new-head')

    def test_source_becoming_ineligible_closes_only_the_mirror(self):
        for changes in ({'draft': True}, {'base': {'ref': 'release'}}, {'state': 'closed'},
                        {'user': {'type': 'User'}}):
            for mode in ('sync', 'dry-run', 'export'):
                with self.subTest(changes=changes, mode=mode):
                    mirror = self.make_mirror(no_open_pr=mode == 'export')
                    mirror.dry_run = mode == 'dry-run'
                    mirror.mirrors[1] = {'state': 'open', 'number': 2}
                    fixture = mirror.api.call.side_effect
                    mirror.api.call.side_effect = lambda repo, path: dict(fixture(repo, path), **changes)
                    result = mirror.sync(1)
                    if mode == 'sync':
                        self.assertEqual(result, {'status': 'closed'})
                        mirror.api.write.assert_called_once_with('pulls/2', {'state': 'closed'}, 'PATCH')
                    else:
                        mirror.api.write.assert_not_called()
                    mirror.commit.assert_not_called()

    def test_no_dependency_changes_closes_open_mirror_without_comment(self):
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run):
                mirror = self.make_mirror(no_open_pr=False)
                mirror.dry_run = dry_run
                mirror.mirrors[1] = {'state': 'open', 'number': 2,
                                     'base': {'sha': 'baseline', 'ref': 'shadow/base/1'}, 'head': {'ref': 'shadow/pr/1'}}
                mirror.api.pages.return_value = [{'filename': 'README.md'}]
                result = mirror.sync(1)
                self.assertEqual(result['status'], 'dry-run' if dry_run else 'closed_no_deps')
                if dry_run:
                    mirror.api.write.assert_not_called()
                else:
                    mirror.api.write.assert_called_once_with('pulls/2', {'state': 'closed'}, 'PATCH')
                mirror.commit.assert_not_called()

    def test_renames_check_old_and_new_paths(self):
        for filename, previous, expected in [('moved.json', 'package.json', 'dry-run'),
                                              ('package.json', 'moved.json', 'dry-run'),
                                              ('other.json', 'old.json', 'skipped'),
                                              ('other.json', WORKFLOW, 'skipped')]:
            with self.subTest(filename=filename, previous=previous):
                mirror = self.make_mirror()
                mirror.dry_run = True
                mirror.api.pages.return_value = [{'filename': filename, 'previous_filename': previous}]
                self.assertEqual(mirror.sync(1)['status'], expected)

    def test_bad_message_is_replaced_with_same_tree_and_parent(self):
        mirror = self.make_mirror()
        original_call = mirror.api.call.side_effect
        def call(repo, path):
            result = original_call(repo, path)
            if path == 'pulls/1':
                result['title'] = 'Bump @scope/name'
            return result
        mirror.api.call.side_effect = call
        with TemporaryDirectory() as directory, patch('mirror.Path', return_value=Path(directory)):
            mirror.sync(1)
        mirror.commit.assert_called_once_with('head-tree', ['baseline'], 'Bump `@scope/name`')
        mirror.ref.assert_called_once_with('shadow/pr/1', 'new-head', force=True)

    def test_renamed_manifest_is_reported_even_if_destination_is_not_a_manifest(self):
        mirror = self.make_mirror()
        mirror.api.pages.return_value = [{'filename': 'archived.json', 'previous_filename': 'package.json'}]
        with TemporaryDirectory() as directory, patch('mirror.Path', return_value=Path(directory)):
            result = mirror.sync(1)
            body = Path(result['body_file']).read_text()
        self.assertIn('`package.json` → `archived.json` (not installed by recorder)', body)

    def test_force_update_is_blocked_by_any_open_pr(self):
        mirror = self.make_mirror()
        mirror.api.pages.return_value = [{'number': 2}]
        with self.assertRaisesRegex(RuntimeError, 'open mirror pull request'):
            Mirror.ref(mirror, 'shadow/pr/1', 'new-head', force=True)
        mirror.api.write.assert_not_called()

    def test_force_update_allowed_without_open_pr(self):
        mirror = self.make_mirror()
        mirror.api.pages.return_value = []
        Mirror.ref(mirror, 'shadow/pr/1', 'new-head', force=True)
        mirror.api.write.assert_called_once_with('git/refs/heads/shadow/pr/1', {'sha': 'new-head', 'force': True}, 'PATCH')

    def test_all_dependency_files_and_install_membership_are_exported(self):
        mirror = self.make_mirror()
        mirror.api.pages.return_value.extend([
            {'filename': 'services/mcp/package.json', 'patch': '- "@scope/name": "npm:@scope/pkg@1"\n+ "@scope/name": "npm:@scope/pkg@2"'},
            {'filename': 'nodejs/package.json', 'patch': '- "lib": "1"\n+ "lib": "2"'},
            {'filename': 'other/go.mod', 'patch': '-example.com/lib v1.0.0\n+example.com/lib v2.0.0'},
            {'filename': '.changeset/dependency-update.md', 'patch': ''},
        ])
        with TemporaryDirectory() as directory, patch('mirror.Path', return_value=Path(directory)):
            result = mirror.sync(1)
            body = Path(result['body_file']).read_text()
        self.assertIn('`services/mcp/package.json` (recorder installs)', body)
        self.assertIn('`nodejs/package.json` (recorder installs)', body)
        self.assertIn('`other/go.mod` (not installed by recorder)', body)
        self.assertIn('`@scope/name` `npm:@scope/pkg@1` → `npm:@scope/pkg@2`', body)
        self.assertNotIn('.changeset/dependency-update.md', body)

    def test_dry_run_does_not_write_files_or_refs(self):
        mirror = self.make_mirror()
        mirror.dry_run = True
        with patch('mirror.Path') as path:
            self.assertEqual(mirror.sync(1)['status'], 'dry-run')
        path.assert_not_called()
        mirror.commit.assert_not_called()
        mirror.api.write.assert_not_called()

    def test_replays_node_upgrade_using_the_recorders_version_file(self):
        mirror = self.make_mirror()
        mirror.patterns = INSTALL_PATTERNS
        mirror.api.pages.return_value = [
            {'filename': '.tool-versions', 'patch': '@@\n-nodejs 22.23.3\n+nodejs 22.23.4\n python 3.11'},
            {'filename': 'codegen.Dockerfile'},
        ]
        with TemporaryDirectory() as directory, patch('mirror.Path', return_value=Path(directory)):
            result = mirror.sync(1)
            body = Path(result['body_file']).read_text()
        self.assertIn('`.tool-versions` (recorder installs)', body)
        self.assertIn('Node.js 22.23.3 → 22.23.4', body)

    def test_workspace_summary_reads_sections_and_keeps_exact_selector_and_git_ref(self):
        mirror = self.make_mirror()
        mirror.patterns = INSTALL_PATTERNS
        mirror.api.pages.return_value = [{
            'filename': 'pnpm-workspace.yaml',
            'patch': ' overrides:\n-  "pkg@1.2.3": "github:org/pkg#old"\n'
                     '+  "pkg@1.2.3": "github:org/pkg#new"\n'
                     '-minimumReleaseAge: 60\n+minimumReleaseAge: 120',
        }]
        documents = {
            'source-merge-base': 'minimumReleaseAge: 60\ncatalogs:\n  tools:\n    vite: 1\n'
                                 'overrides:\n  "pkg@1.2.3": "github:org/pkg#old"\n'
                                 '  "other-pkg@2.3.4": "git+https://example.com/other.git#123"\n',
            'source-head': 'minimumReleaseAge: 120\ncatalogs:\n  tools:\n    vite: "2"\n'
                           'overrides:\n  "pkg@1.2.3": "github:org/pkg#new"\n'
                           '  "other-pkg@2.3.4": "git+https://example.com/other.git#refs/heads/next"\n',
        }
        fixture = mirror.api.call.side_effect

        def call(repo, path):
            if repo == SOURCE and path.startswith('contents/pnpm-workspace.yaml?ref='):
                document = documents[path.split('=', 1)[1]]
                return {'content': base64.b64encode(document.encode()).decode()}
            return fixture(repo, path)

        mirror.api.call.side_effect = call
        with TemporaryDirectory() as directory, patch('mirror.Path', return_value=Path(directory)):
            result = mirror.sync(1)
            body = Path(result['body_file']).read_text()
        self.assertIn('pkg@1.2.3', body)
        self.assertIn('github:org/pkg#old → github:org/pkg#new', body)
        self.assertIn('`#123` → `#refs/heads/next`', body)
        self.assertNotIn('https://example.com', body)
        self.assertIn('vite 1 → 2', body)
        self.assertNotIn('minimumReleaseAge', body)

    def test_unused_tool_version_bumps_are_listed_without_install_claim(self):
        for name, old, new in [('bun', '1.4.4', '1.4.5'), ('deno', '2.9.8', '2.9.9'),
                               ('pnpm', '10.34.5', '10.34.6')]:
            with self.subTest(name=name):
                mirror = self.make_mirror()
                mirror.patterns = INSTALL_PATTERNS
                mirror.api.pages.return_value = [
                    {'filename': '.tool-versions', 'patch': f'@@\n-{name} {old}\n+{name} {new}'},
                ]
                with TemporaryDirectory() as directory, patch('mirror.Path', return_value=Path(directory)):
                    result = mirror.sync(1)
                    body = Path(result['body_file']).read_text()
                self.assertIn('`.tool-versions` (not installed by recorder)', body)
                self.assertIn(f'{name} {old} → {new}', body)

    def test_tool_version_recorder_coverage_is_unknown_when_patch_is_omitted(self):
        mirror = self.make_mirror()
        mirror.patterns = INSTALL_PATTERNS
        mirror.api.pages.return_value = [
            {'filename': '.tool-versions'},
            {'filename': 'pnpm-lock.yaml'},
        ]
        with TemporaryDirectory() as directory, patch('mirror.Path', return_value=Path(directory)):
            result = mirror.sync(1)
            body = Path(result['body_file']).read_text()
        self.assertIn('`.tool-versions` (recorder coverage unknown)', body)
        self.assertIn('`pnpm-lock.yaml` (recorder installs)', body)

    def test_export_error_does_not_open_pr(self):
        mirror = self.make_mirror()
        with patch('mirror.Path') as path:
            path.return_value.mkdir.side_effect = OSError('Export unavailable')
            with self.assertRaisesRegex(OSError, 'Export unavailable'):
                mirror.sync(1)
        mirror.api.write.assert_not_called()


class VersionsTest(unittest.TestCase):
    def test_dependency_file_types(self):
        for path in ['a/package.json', 'pnpm-lock.yaml', 'a/requirements-dev.txt', 'a/go.mod', 'go.sum', 'Cargo.toml', 'Cargo.lock', '.node-version', '.nvmrc', 'uv.lock']:
            self.assertTrue(dependency_file(path), path)
        self.assertFalse(dependency_file('README.md'))
        self.assertFalse(dependency_file(''))

    def test_workspace_membership_excludes_independent_projects(self):
        patterns = ['package.json']
        workspace = ['products/*', '!products/mobile', 'services/mcp']
        self.assertTrue(recorder_installs('services/mcp/package.json', patterns, workspace))
        self.assertFalse(recorder_installs('products/mobile/package.json', patterns, workspace))
        self.assertFalse(recorder_installs('other/go.sum', patterns, workspace))

    def test_pnpm_lock(self):
        patch = "-  '@scope/pkg@1.0.0':\n+  '@scope/pkg@2.0.0':\n-  lib@1.0.0:\n+  lib@2.0.0:\n-      '@scope/pkg': 1.0.0\n+      '@scope/pkg': 2.0.0"
        self.assertEqual(version_transitions('pnpm-lock.yaml', patch), [('@scope/pkg', '1.0.0', '2.0.0'), ('lib', '1.0.0', '2.0.0')])

    def test_pnpm_workspace_double_quoted_catalog_entry(self):
        documents = ('catalog:\n  "@types/node": "^22.20.5"', 'catalog:\n  "@types/node": "^22.20.6"')
        self.assertEqual(version_transitions('pnpm-workspace.yaml', None, workspace_documents=documents),
                         [('@types/node', '^22.20.5', '^22.20.6')])

    def test_pnpm_workspace_catalog_bump_across_quote_styles(self):
        for name, plain, double in [('vite', 'vite: ^{version}', '"vite": "^{version}"'),
                                    ('@types/node', "'@types/node': '^{version}'", '"@types/node": "^{version}"')]:
            for before, after in [(plain, double), (double, plain)]:
                with self.subTest(before=before, after=after):
                    documents = (f"catalog:\n  {before.format(version='1.0.0')}",
                                 f"catalog:\n  {after.format(version='1.0.1')}")
                    self.assertEqual(version_transitions('pnpm-workspace.yaml', None, workspace_documents=documents),
                                     [(name, '^1.0.0', '^1.0.1')])

    def test_pnpm_range_overrides(self):
        patch = (
            " overrides:\n"
            "-  rollup@>=4: '>=4.59.0'\n+  rollup@>=4: '>=4.60.0'\n"
            "-  '@babel/core@>=7.0.0 <7.29.6': ^7.29.6\n+  '@babel/core@>=7.0.0 <7.29.6': ^7.29.7"
        )
        expected = [('@babel/core@>=7.0.0 <7.29.6', '^7.29.6', '^7.29.7'), ('rollup@>=4', '>=4.59.0', '>=4.60.0')]
        documents = ("overrides:\n  rollup@>=4: '>=4.59.0'\n  '@babel/core@>=7.0.0 <7.29.6': ^7.29.6",
                     "overrides:\n  rollup@>=4: '>=4.60.0'\n  '@babel/core@>=7.0.0 <7.29.6': ^7.29.7")
        self.assertEqual(version_transitions('pnpm-workspace.yaml', None, workspace_documents=documents), expected)
        self.assertEqual(version_transitions('pnpm-lock.yaml', patch), expected)

    def test_pnpm_workspace_exact_overrides_preserve_git_ref_scalars(self):
        documents = ('overrides: {"pkg@1.2.3": "git+https://host/pkg.git#v1"}',
                     'overrides:\n  pkg@1.2.3: git+https://host/pkg.git#v2 # default branch')
        self.assertEqual(version_transitions('pnpm-workspace.yaml', None, workspace_documents=documents),
                         [('pkg@1.2.3', 'git+https://host/pkg.git#v1', 'git+https://host/pkg.git#v2')])

    def test_go_rust_python_locks_and_node(self):
        self.assertEqual(version_transitions('go.mod', '-example.com/lib v1.0.0\n+example.com/lib v2.0.0'), [('example.com/lib', 'v1.0.0', 'v2.0.0')])
        for filename in ['Cargo.lock', 'uv.lock']:
            self.assertEqual(version_transitions(filename, ' [[package]]\n name = "lib"\n-version = "1"\n+version = "2"'), [('lib', '1', '2')])
        self.assertEqual(version_transitions('.nvmrc', '-20\n+22'), [('Node.js', '20', '22')])

    def test_json(self):
        self.assertEqual(version_transitions('package.json', '@@\n- "pnpm": "9",\n+ "pnpm": "10",'), [('pnpm', '9', '10')])

    def test_python(self):
        self.assertEqual(version_transitions('pyproject.toml', '- "django>=4.0",\n+ "django>=5.0",'), [('django', '>=4.0', '>=5.0')])
        self.assertEqual(version_transitions('requirements.txt', '-django==4.0\n+django==5.0'), [('django', '==4.0', '==5.0')])
        self.assertEqual(version_transitions('pyproject.toml', '-django = "4.0"\n+django = "5.0"'), [('django', '4.0', '5.0')])

    def test_empty_unchanged_and_malformed(self):
        self.assertEqual(version_transitions('package.json', None), [])
        self.assertEqual(version_transitions('package.json', '-broken\n+also broken'), [])
        self.assertEqual(version_transitions('pnpm-lock.yaml', '-a==1\n+a==2'), [])
        self.assertEqual(version_transitions('requirements.txt', '-django==4\n+django==4'), [])


if __name__ == '__main__':
    unittest.main()
