"""Offline deployment checks: no Git writes or hosted API calls."""

import json
import hashlib
import os
import sqlite3
from contextlib import closing, nullcontext
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

    def test_migration_change_can_fast_forward_for_verified_backup_and_migration(self):
        replies = [str(deploy.ROOT), 'main', '', '', SHA, '', 'backend/migrations/012_local_similarity.sql', '', SHA]
        with patch.object(deploy.Path, 'cwd', return_value=deploy.ROOT), \
                patch.object(deploy.Path, 'resolve', return_value=deploy.ROOT), \
                patch.object(deploy, 'git', side_effect=replies) as git, \
                patch.object(deploy.time, 'time', return_value=0):
            deploy.update_checkout(SHA, 100)
        self.assertIn(unittest.mock.call('merge', '--ff-only', '--no-overwrite-ignore', SHA), git.call_args_list)

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

    def test_remote_failure_reports_safe_stage_reason_and_http_status(self):
        success = self.response(result=dict(run_id=RUN_ID, sha=SHA, success=True))
        cases = (
            ([self.response(403)], 'stage=console_input reason=http_rejected http_status=403'),
            ([self.response(), self.response(500)], 'stage=confirmation reason=http_rejected http_status=500'),
            ([self.response(), success, self.response(502)], 'stage=web_reload reason=http_rejected http_status=502'),
            ([deploy.requests.Timeout('private-token')], 'stage=console_input reason=network_error http_status=unavailable'),
        )
        for responses, expected in cases:
            with self.subTest(expected=expected), self.assertLogs(deploy.logger, level='ERROR') as logs:
                result, _ = self.run_deploy(responses)
            self.assertEqual(result, 1)
            self.assertIn(expected, ' '.join(logs.output))
            self.assertNotIn('private-token', ' '.join(logs.output))

    def test_polling_is_bounded_and_never_reloads_without_confirmation(self):
        with self.assertLogs(deploy.logger, level='ERROR') as logs:
            result, session = self.run_deploy([self.response()] + [self.response(404) for _ in range(36)])
        self.assertEqual(result, 1)
        self.assertEqual(session.request.call_count, 37)
        self.assertIn('stage=confirmation reason=deadline_expired', ' '.join(logs.output))

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
                        patch.object(deploy, 'update_checkout', side_effect=error), \
                        patch.object(deploy, 'migrate_pending') as migrate:
                    result = deploy.hosted(SHA, RUN_ID, 100)
                self.assertEqual(result, int(error is not None))
                self.assertEqual(json.loads(status.read_text()), dict(
                    run_id=RUN_ID, sha=SHA, success=error is None,
                    stage=None if error is None else 'checkout',
                    error_code=None if error is None else 'runtime_error'))
                if error is None:
                    migrate.assert_called_once_with(deadline=100, deployment_sha=SHA)
                else:
                    migrate.assert_not_called()
                self.assertFalse(status.with_suffix('.tmp').exists())

    def test_hosted_lock_failure_is_reported_in_status_without_checkout(self):
        with tempfile.TemporaryDirectory(dir='tests') as directory:
            root, status = Path(directory), Path(directory) / 'deployment-status.json'
            with patch.object(deploy, 'ROOT', root), patch.object(deploy, 'STATUS', status), \
                    patch('scripts.run_pipeline.exclusive_cycle', side_effect=RuntimeError('private lock detail')), \
                    patch.object(deploy, 'update_checkout') as update:
                self.assertEqual(deploy.hosted(SHA, RUN_ID, 100), 1)
            self.assertEqual(json.loads(status.read_text()), dict(
                run_id=RUN_ID, sha=SHA, success=False, stage='lock', error_code='runtime_error'))
            update.assert_not_called()

    def test_safe_migration_failure_code_is_reported_without_reload(self):
        status = dict(run_id=RUN_ID, sha=SHA, success=False,
                      stage='migrations', error_code='backup_integrity_check_failed')
        result, session = self.run_deploy([self.response(), self.response(result=status)])
        self.assertEqual(result, 1)
        self.assertEqual(session.request.call_count, 2)
        self.assertTrue(all(not call.args[1].endswith('/reload/') for call in session.request.call_args_list))

    def test_pending_migration_is_backed_up_then_applied_and_verified(self):
        with tempfile.TemporaryDirectory(dir='tests') as directory:
            root = Path(directory)
            database = root / 'data/jobs.db'
            migration_dir = root / 'backend/migrations'
            backup_dir = root / 'data/backups'
            migration_dir.mkdir(parents=True)
            database.parent.mkdir(parents=True, exist_ok=True)
            sql = 'CREATE TABLE local_feature (value TEXT);'
            migration = migration_dir / '012_local_similarity.sql'
            migration.write_text(sql, encoding='utf-8')
            with closing(sqlite3.connect(database)) as conn, conn:
                conn.execute('CREATE TABLE preserved (value TEXT)')
                conn.execute("INSERT INTO preserved VALUES('kept')")
            checksum = hashlib.sha256(sql.encode('utf-8')).hexdigest()

            def apply_migration(command, **kwargs):
                self.assertEqual(command[-3:], ['-B', '-m', 'backend.migrate'])
                self.assertEqual(kwargs['cwd'], deploy.ROOT)
                self.assertLessEqual(kwargs['timeout'], deploy.MAX_MIGRATION_SECONDS)
                with closing(sqlite3.connect(database)) as conn, conn:
                    conn.execute('CREATE TABLE schema_migrations (name TEXT PRIMARY KEY,checksum TEXT NOT NULL)')
                    conn.execute(sql)
                    conn.execute('INSERT INTO schema_migrations VALUES(?,?)', (migration.name, checksum))
                return MagicMock(returncode=0)

            with patch.object(deploy, 'ROOT', root), patch.object(deploy.subprocess, 'run', side_effect=apply_migration):
                applied = deploy.migrate_pending(database, migration_dir, backup_dir,
                                                 deadline=deploy.time.time() + 100, deployment_sha=SHA)

            self.assertEqual(applied, [migration.name])
            backups = list(backup_dir.glob('jobs-before-deploy-*.db'))
            self.assertEqual(len(backups), 1)
            with closing(sqlite3.connect(backups[0])) as conn:
                self.assertEqual(conn.execute('PRAGMA quick_check').fetchone()[0], 'ok')
                self.assertEqual(conn.execute('SELECT value FROM preserved').fetchone()[0], 'kept')
                self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='local_feature'").fetchone())
            self.assertEqual(deploy.pending_migrations(database, migration_dir), [])

    def test_changed_applied_migration_fails_before_backup_or_command(self):
        with tempfile.TemporaryDirectory(dir='tests') as directory:
            root = Path(directory)
            database = root / 'jobs.db'
            migrations = root / 'migrations'
            migrations.mkdir()
            migration = migrations / '001.sql'
            migration.write_text('CREATE TABLE local_feature(value TEXT);', encoding='utf-8')
            with closing(sqlite3.connect(database)) as conn, conn:
                conn.execute('CREATE TABLE schema_migrations(name TEXT PRIMARY KEY,checksum TEXT NOT NULL)')
                conn.execute('INSERT INTO schema_migrations VALUES(?,?)', ('001.sql', 'wrong'))
            with patch.object(deploy.subprocess, 'run') as run:
                with self.assertRaisesRegex(RuntimeError, 'applied migration was modified'):
                    deploy.migrate_pending(database, migrations, root / 'backups', deadline=100)
            run.assert_not_called()
            self.assertFalse((root / 'backups').exists())

    def test_invalid_and_oversized_status_never_reload(self):
        oversized = self.response()
        oversized.iter_content.return_value = [b'x' * 1025]
        for response in (oversized, self.response(result=[])):
            with self.subTest(response=response):
                result, session = self.run_deploy([self.response(), response])
                self.assertEqual(result, 1)
                self.assertEqual(session.request.call_count, 2)

