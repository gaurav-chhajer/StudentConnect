"""Offline checks for console-free hosted release activation."""

import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import zipfile

from scripts import deploy_pythonanywhere_api as api
from scripts import deploy_pythonanywhere as migration
from scripts import hosted_release as release

SHA = 'a' * 40
RUN = 'b' * 32


class ReleaseTests(unittest.TestCase):
    def request(self, archive_hash='c' * 64):
        return {'sha': SHA, 'archive_sha256': archive_hash, 'run_id': RUN}

    def test_invalid_metadata_and_archive_do_not_activate(self):
        with tempfile.TemporaryDirectory(dir='tests') as directory:
            root = Path(directory)
            data = root / 'data'
            data.mkdir()
            archive = data / 'deploy-release.zip'
            archive.write_bytes(b'not a release')
            with patch.object(release, 'ROOT', root), patch.object(release, 'DATA', data), \
                    patch.object(release, 'RELEASES', root / '.releases'), patch.object(release, 'ARCHIVE', archive):
                with self.assertRaisesRegex(ValueError, 'checksum'):
                    release._extract(self.request())
                self.assertFalse((root / '.releases').exists())
                with self.assertRaisesRegex(ValueError, 'revision'):
                    release._metadata({'sha': '../outside'})

    def test_valid_archive_extracts_only_inside_versioned_release(self):
        with tempfile.TemporaryDirectory(dir='tests') as directory:
            root = Path(directory)
            data = root / 'data'
            data.mkdir()
            archive_path = data / 'deploy-release.zip'
            output = BytesIO()
            with zipfile.ZipFile(output, 'w') as archive:
                for name in ('backend/app.py', 'backend/migrate.py',
                             'frontend/templates/index.html', 'frontend/static/main.css'):
                    archive.writestr(name, 'safe content')
            payload = output.getvalue()
            archive_path.write_bytes(payload)
            with patch.object(release, 'ROOT', root), patch.object(release, 'DATA', data), \
                    patch.object(release, 'RELEASES', root / '.releases'), \
                    patch.object(release, 'ARCHIVE', archive_path), \
                    patch.object(Path, 'symlink_to') as symlink:
                target = release._extract(self.request(hashlib.sha256(payload).hexdigest()))
            self.assertEqual(target, root / '.releases' / SHA)
            self.assertEqual((target / 'backend/app.py').read_text(), 'safe content')
            self.assertEqual(symlink.call_count, 2)

    def test_traversal_entry_is_rejected_before_activation(self):
        with tempfile.TemporaryDirectory(dir='tests') as directory:
            root = Path(directory)
            data = root / 'data'
            data.mkdir()
            archive_path = data / 'deploy-release.zip'
            output = BytesIO()
            with zipfile.ZipFile(output, 'w') as archive:
                archive.writestr('backend/../../outside.py', 'unsafe')
            payload = output.getvalue()
            archive_path.write_bytes(payload)
            with patch.object(release, 'ROOT', root), patch.object(release, 'DATA', data), \
                    patch.object(release, 'RELEASES', root / '.releases'), \
                    patch.object(release, 'ARCHIVE', archive_path):
                with self.assertRaisesRegex(ValueError, 'unsafe path'):
                    release._extract(self.request(hashlib.sha256(payload).hexdigest()))
            self.assertFalse((root / '.releases' / SHA).exists())
            self.assertFalse((root / 'outside.py').exists())

    def test_migration_helper_comes_from_release_not_old_host_checkout(self):
        with tempfile.TemporaryDirectory(dir='tests') as directory:
            root = Path(directory)
            helper = root / 'scripts/deploy_pythonanywhere.py'
            helper.parent.mkdir()
            helper.write_text('def migrate_pending(*, runtime_root):\n    return runtime_root\n', encoding='utf-8')
            with patch.object(api.legacy, 'migrate_pending', side_effect=AssertionError('old host helper used')):
                module = release._migration_module(root)
                self.assertEqual(module.migrate_pending(runtime_root=root), root)

    def test_preflight_uses_configured_virtualenv_python(self):
        with tempfile.TemporaryDirectory(dir='tests') as directory:
            root = Path(directory)
            python = root / '.venv/bin/python'
            python.parent.mkdir(parents=True)
            python.write_bytes(b'local test executable placeholder')
            candidate = root / '.releases' / SHA
            with patch.object(release, 'ROOT', root), patch.object(release.subprocess, 'run') as command:
                command.return_value.returncode = 0
                release._preflight(candidate)
            self.assertEqual(command.call_args.args[0][0], str(python))
            self.assertEqual(command.call_args.kwargs['cwd'], root)

    def test_failed_preparation_keeps_previous_release_and_records_safe_status(self):
        with tempfile.TemporaryDirectory(dir='tests') as directory:
            root = Path(directory)
            data = root / 'data'
            data.mkdir()
            (data / 'active-release.json').write_text(json.dumps({'sha': 'c' * 40}), encoding='utf-8')
            (data / 'deploy-request.json').write_text(json.dumps(self.request()), encoding='utf-8')
            old_app = object()
            with patch.object(release, 'ROOT', root), patch.object(release, 'DATA', data), \
                    patch.object(release, 'RELEASES', root / '.releases'), \
                    patch.object(release, 'ACTIVE', data / 'active-release.json'), \
                    patch.object(release, 'REQUEST', data / 'deploy-request.json'), \
                    patch.object(release, 'STATUS', data / 'deployment-status.json'), \
                    patch.object(release, '_pipeline_lock', side_effect=OSError('private detail')), \
                    patch.object(release, '_import_application', return_value=old_app) as imported:
                with self.assertLogs(release.logger, level='ERROR') as logs:
                    self.assertIs(release.application(), old_app)
            self.assertNotIn('private detail', ' '.join(logs.output))
            self.assertEqual(imported.call_args.args[0], root / '.releases' / ('c' * 40))
            self.assertEqual(json.loads((data / 'active-release.json').read_text())['sha'], 'c' * 40)
            status = json.loads((data / 'deployment-status.json').read_text())
            self.assertEqual(status, {'run_id': RUN, 'sha': SHA, 'success': False,
                                      'stage': 'extract', 'error_code': 'OSError'})


class ApiTests(unittest.TestCase):
    def response(self, status=200, content=b''):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status_code = status
        response.iter_content.return_value = [content]
        return response

    def test_upload_order_and_verified_activation(self):
        body = b'archive'
        status = json.dumps({'run_id': RUN, 'sha': SHA, 'success': True, 'stage': None}).encode()
        requirements = (api.ROOT / 'requirements.txt').read_bytes()
        responses = [self.response(content=requirements), self.response(404),
                     self.response(content=b'previous entry'), self.response(201),
                     self.response(201), self.response(201), self.response(201),
                     self.response(200), self.response(200), self.response(content=status)]
        session = MagicMock()
        session.__enter__.return_value = session
        session.request.side_effect = responses
        with patch.dict(os.environ, {'PYTHONANYWHERE_API_TOKEN': 'synthetic-token'}), \
                patch.object(api, 'release_archive', return_value=body), \
                patch.object(api.requests, 'Session', return_value=session), \
                patch.object(api.uuid, 'uuid4') as uuid:
            uuid.return_value.hex = RUN
            self.assertEqual(api.deploy(SHA), 0)
        calls = session.request.call_args_list
        self.assertEqual([call.args[0] for call in calls],
                         ['GET', 'GET', 'GET', 'POST', 'POST', 'POST', 'POST', 'POST', 'POST', 'GET'])
        self.assertIn('deployment-backups', calls[3].args[1])
        self.assertIn('hosted_release.py', calls[4].args[1])
        self.assertIn('deploy-release.zip', calls[5].args[1])
        self.assertIn('deploy-request.json', calls[6].args[1])
        self.assertIn('wsgi.py', calls[7].args[1])
        self.assertIn('/reload/', calls[8].args[1])
        metadata = json.loads(calls[6].kwargs['files']['content'][1])
        self.assertEqual(metadata['archive_sha256'], hashlib.sha256(body).hexdigest())

    def reload_session(self, reload_response, statuses):
        session = MagicMock()
        session.__enter__.return_value = session
        requirements = (api.ROOT / 'requirements.txt').read_bytes()
        session.request.side_effect = [
            self.response(content=requirements), self.response(404),
            self.response(content=b'previous entry'), self.response(201),
            self.response(201), self.response(201), self.response(201), self.response(200),
            reload_response, *statuses]
        return session

    def deploy_mock_session(self, session, clock):
        with patch.dict(os.environ, {'PYTHONANYWHERE_API_TOKEN': 'synthetic-token'}), \
                patch.object(api, 'release_archive', return_value=b'archive'), \
                patch.object(api.requests, 'Session', return_value=session), \
                patch.object(api.uuid, 'uuid4') as uuid, \
                patch.object(api.time, 'monotonic', side_effect=clock), \
                patch.object(api.time, 'sleep'):
            uuid.return_value.hex = RUN
            try:
                return api.deploy(SHA)
            finally:
                reloads = [call for call in session.request.call_args_list if '/reload/' in call.args[1]]
                self.assertEqual(len(reloads), 1)
                self.assertEqual(reloads[0].args[0], 'POST')
                self.assertEqual(reloads[0].kwargs['timeout'], (10, 30))
                self.assertTrue(all(call.args[0] == 'GET' for call in session.request.call_args_list[9:]))

    def test_uncertain_reload_confirms_matching_activation_without_resending(self):
        status = self.response(content=json.dumps({'run_id': RUN, 'sha': SHA, 'success': True}).encode())
        for error_type in (api.requests.ReadTimeout, api.requests.ConnectionError):
            with self.subTest(error=error_type.__name__):
                error = error_type('private-token-url')
                error.__cause__ = OSError('private-path')
                session = self.reload_session(error, [status])
                with self.assertLogs(api.logger, level='INFO') as logs:
                    self.assertEqual(self.deploy_mock_session(session, [0, 0]), 0)
                output = '\n'.join(logs.output)
                self.assertIn('error=' + error_type.__name__, output)
                self.assertIn('cause=OSError', output)
                self.assertIn('reload_acknowledged=False', output)
                self.assertNotIn('private-token-url', output)
                self.assertNotIn('private-path', output)
                self.assertNotIn('synthetic-token', output)
                self.assertEqual(session.request.call_count, 10)

    def test_uncertain_reload_ignores_old_status_until_current_run_is_confirmed(self):
        old = self.response(content=json.dumps({'run_id': 'c' * 32, 'sha': SHA, 'success': True}).encode())
        current = self.response(content=json.dumps({'run_id': RUN, 'sha': SHA, 'success': True}).encode())
        session = self.reload_session(api.requests.ReadTimeout(), [old, current])
        self.assertEqual(self.deploy_mock_session(session, [0, 0, 1]), 0)
        self.assertEqual(session.request.call_count, 11)

    def test_uncertain_reload_missing_or_old_status_expires_without_success(self):
        for response in (self.response(404), self.response(content=json.dumps(
                {'run_id': 'c' * 32, 'sha': SHA, 'success': True}).encode())):
            with self.subTest(status=response.status_code):
                session = self.reload_session(api.requests.ReadTimeout(), [response])
                with self.assertRaisesRegex(api.legacy.RemoteDeploymentError, 'deadline_expired') as raised:
                    self.deploy_mock_session(session, [0, 0, 181])
                self.assertEqual(raised.exception.stage, 'confirmation')
                self.assertEqual(session.request.call_count, 10)

    def test_uncertain_reload_cannot_accept_wrong_sha_or_invalid_status(self):
        for status in ({'run_id': RUN, 'sha': 'd' * 40, 'success': True},
                       {'run_id': RUN, 'sha': SHA, 'success': 'true'}):
            with self.subTest(status=status):
                session = self.reload_session(api.requests.ReadTimeout(), [
                    self.response(content=json.dumps(status).encode())])
                with self.assertRaisesRegex(api.legacy.RemoteDeploymentError, 'invalid_status'):
                    self.deploy_mock_session(session, [0, 0])

    def test_uncertain_reload_reports_confirmed_activation_failure(self):
        status = {'run_id': RUN, 'sha': SHA, 'success': False,
                  'stage': 'startup', 'error_code': 'RuntimeError'}
        session = self.reload_session(api.requests.ReadTimeout(), [
            self.response(content=json.dumps(status).encode())])
        self.assertEqual(self.deploy_mock_session(session, [0, 0]), 1)

    def test_reload_http_rejection_stops_without_status_polling(self):
        session = self.reload_session(self.response(502), [])
        with self.assertRaisesRegex(api.legacy.RemoteDeploymentError, 'http_rejected') as raised:
            self.deploy_mock_session(session, [])
        self.assertEqual(raised.exception.http_status, 502)
        self.assertEqual(session.request.call_count, 9)

    def test_confirmation_network_failure_propagates_without_retry(self):
        session = self.reload_session(api.requests.ReadTimeout(), [api.requests.ConnectionError('private')])
        with self.assertRaisesRegex(api.legacy.RemoteDeploymentError, 'network_error') as raised:
            self.deploy_mock_session(session, [0, 0])
        self.assertEqual(raised.exception.stage, 'confirmation')
        self.assertEqual(session.request.call_count, 10)

    def test_dependency_mismatch_stops_before_upload(self):
        session = MagicMock()
        session.__enter__.return_value = session
        session.request.return_value = self.response(content=b'different requirements')
        with patch.dict(os.environ, {'PYTHONANYWHERE_API_TOKEN': 'synthetic-token'}), \
                patch.object(api, 'release_archive', return_value=b'archive'), \
                patch.object(api.requests, 'Session', return_value=session):
            with self.assertRaisesRegex(api.legacy.RemoteDeploymentError, 'manual_dependency_install_required'):
                api.deploy(SHA)
        self.assertEqual(session.request.call_count, 1)

    def test_migration_command_runs_from_release_directory(self):
        runtime_root = Path('tests/release-simulation')
        with patch.object(migration, 'pending_migrations', side_effect=[[Path('013.sql')], []]), \
                patch.object(migration, 'backup_database'), \
                patch.object(migration.subprocess, 'run') as command:
            applied = migration.migrate_pending(database=Path('tests/jobs.db'),
                                                migration_dir=runtime_root / 'backend/migrations',
                                                deadline=migration.time.time() + 30,
                                                runtime_root=runtime_root,
                                                python_executable=Path('tests/venv/python'))
        self.assertEqual(applied, ['013.sql'])
        self.assertEqual(command.call_args.kwargs['cwd'], runtime_root)
        self.assertEqual(command.call_args.args[0][0], str(Path('tests/venv/python')))


if __name__ == '__main__':
    unittest.main()
