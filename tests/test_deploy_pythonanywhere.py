"""Offline deployment checks: no Git writes or hosted API calls."""

import json
import os
from contextlib import nullcontext
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from scripts import deploy_pythonanywhere as deploy

SHA = 'a' * 40
RUN_ID = 'b' * 32


class CheckoutTests(unittest.TestCase):
    def test_clean_checkout_fast_forwards_exact_revision(self):
        replies = [str(deploy.ROOT), 'main', '', '', SHA, '', 'frontend/templates/index.html', '', SHA]
        with patch.object(deploy.Path, 'cwd', return_value=deploy.ROOT), \
                patch.object(deploy.Path, 'resolve', return_value=deploy.ROOT), \
                patch.object(deploy, 'git', side_effect=replies) as git, \
                patch.object(deploy.time, 'time', return_value=0):
            deploy.update_checkout(SHA, 100)
        self.assertIn(unittest.mock.call('merge', '--ff-only', '--no-overwrite-ignore', SHA), git.call_args_list)

    def test_unsafe_checkout_or_change_never_merges(self):
        cases = [
            ([str(deploy.ROOT), 'feature'], 100),
            ([str(deploy.ROOT), 'main', ' M README.md'], 100),
            ([str(deploy.ROOT), 'main', '', '', 'c' * 40], 100),
            ([str(deploy.ROOT), 'main', '', '', SHA, '', 'requirements.txt'], 100),
            ([str(deploy.ROOT), 'main', '', '', SHA, '', 'backend/migrations/999.sql'], 100),
            ([str(deploy.ROOT), 'main', '', '', SHA, '', '.env'], 100),
            ([str(deploy.ROOT), 'main', '', '', SHA, '', 'data/jobs.db'], 100),
            ([str(deploy.ROOT), 'main', '', '', SHA, '', 'frontend/static/main.css'], 0),
        ]
        for replies, deadline in cases:
            with self.subTest(replies=replies), patch.object(deploy.Path, 'cwd', return_value=deploy.ROOT), \
                    patch.object(deploy.Path, 'resolve', return_value=deploy.ROOT), \
                    patch.object(deploy, 'git', side_effect=replies) as git, \
                    patch.object(deploy.time, 'time', return_value=0):
                with self.assertRaises(RuntimeError):
                    deploy.update_checkout(SHA, deadline)
                self.assertFalse(any(call.args[0] == 'merge' for call in git.call_args_list))

    def test_git_subprocess_has_timeout_and_captures_private_output(self):
        with patch.object(deploy.subprocess, 'run') as run:
            deploy.git('status', '--porcelain')
        self.assertTrue(run.call_args.kwargs['check'])
        self.assertTrue(run.call_args.kwargs['capture_output'])
        self.assertEqual(run.call_args.kwargs['timeout'], 60)
        self.assertEqual(run.call_args.kwargs['env']['GIT_TERMINAL_PROMPT'], '0')


class RemoteTests(unittest.TestCase):
    def response(self, status=200, result=None):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status_code = status
        response.iter_content.return_value = [json.dumps(result).encode()]
        return response

    def run_deploy(self, responses):
        session = MagicMock()
        session.__enter__.return_value = session
        session.request.side_effect = responses
        with patch.dict(os.environ, PYTHONANYWHERE_API_TOKEN='synthetic-token', PYTHONANYWHERE_CONSOLE_ID='123'), \
                patch.object(deploy.requests, 'Session', return_value=session), \
                patch.object(deploy.uuid, 'uuid4') as uuid, \
                patch.object(deploy.time, 'time', return_value=0), \
                patch.object(deploy.time, 'sleep'):
            uuid.return_value.hex = RUN_ID
            result = deploy.main(['--sha', SHA])
        return result, session

    def test_reload_only_after_matching_success_status(self):
        responses = [self.response(), self.response(404),
                     self.response(result=dict(run_id='old', sha=SHA, success=True)),
                     self.response(result=dict(run_id=RUN_ID, sha=SHA, success=True)), self.response()]
        result, session = self.run_deploy(responses)
        self.assertEqual(result, 0)
        calls = session.request.call_args_list
        self.assertEqual(sum(call.args[0] == 'POST' for call in calls), 2)
        self.assertTrue(calls[-1].args[1].endswith('/reload/'))
        self.assertIn('--hosted ' + SHA + ' ' + RUN_ID, calls[0].kwargs['data']['input'])
        self.assertFalse(calls[0].kwargs['allow_redirects'])

    def test_failed_status_http_error_and_timeout_never_reload_or_retry(self):
        for responses in ([self.response(403)],
                          [self.response(), self.response(result=dict(run_id=RUN_ID, sha=SHA, success=False))],
                          [self.response(), self.response(result=dict(run_id=RUN_ID, sha='c' * 40, success=True))],
                          [deploy.requests.Timeout('private-token')],
                          [self.response(), self.response(500)]):
            with self.subTest(responses=responses), self.assertLogs(deploy.logger, level='ERROR') as logs:
                result, session = self.run_deploy(responses)
            self.assertEqual(result, 1)
            self.assertEqual(sum(call.args[0] == 'POST' for call in session.request.call_args_list), 1)
            self.assertNotIn('private-token', ' '.join(logs.output))

    def test_polling_is_bounded_and_never_reloads_without_confirmation(self):
        result, session = self.run_deploy([self.response()] + [self.response(404) for _ in range(36)])
        self.assertEqual(result, 1)
        self.assertEqual(session.request.call_count, 37)

    def test_invalid_configuration_and_sha_make_no_calls(self):
        with patch.dict(os.environ, PYTHONANYWHERE_API_TOKEN='', PYTHONANYWHERE_CONSOLE_ID='bad'), \
                patch.object(deploy.requests, 'Session') as session:
            self.assertEqual(deploy.main(['--sha', SHA]), 1)
            self.assertEqual(deploy.main(['--sha', 'main; whoami']), 1)
        session.assert_not_called()

    def test_hosted_result_records_success_or_failure_atomically(self):
        for error in (None, RuntimeError('private-error')):
            with self.subTest(fails=error is not None), tempfile.TemporaryDirectory(dir='tests') as directory:
                root = Path(directory)
                status = root / 'deployment-status.json'
                with patch.object(deploy, 'ROOT', root), patch.object(deploy, 'STATUS', status), \
                        patch('scripts.run_pipeline.exclusive_cycle', return_value=nullcontext()), \
                        patch.object(deploy, 'update_checkout', side_effect=error):
                    result = deploy.hosted(SHA, RUN_ID, 100)
                self.assertEqual(result, int(error is not None))
                self.assertEqual(json.loads(status.read_text()),
                                 dict(run_id=RUN_ID, sha=SHA, success=error is None))
                self.assertFalse(status.with_suffix('.tmp').exists())

    def test_invalid_and_oversized_status_never_reload(self):
        oversized = self.response()
        oversized.iter_content.return_value = [b'x' * 1025]
        for response in (oversized, self.response(result=[])):
            with self.subTest(response=response):
                result, session = self.run_deploy([self.response(), response])
                self.assertEqual(result, 1)
                self.assertEqual(session.request.call_count, 2)

