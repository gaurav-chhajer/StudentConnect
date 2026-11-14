"""Hosting diagnostic calls are bounded, read-only and do not expose contents."""

import json
import unittest
from unittest.mock import MagicMock, patch

from scripts import inspect_pythonanywhere_storage as inspection


class StorageInspectionTests(unittest.TestCase):
    def response(self, payload=None, size=None, status=200):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status_code = status
        response.headers = {} if size is None else {'Content-Length': str(size)}
        response.iter_content.return_value = [json.dumps(payload).encode()]
        return response

    def session(self, backups=None, sizes=None):
        session = MagicMock()
        root = inspection.deployment.ROOT.as_posix()
        backups = backups if backups is not None else [root+'/data/backups/one.db',root+'/data/backups/two.db']
        values = [self.response(backups)]
        values.extend(self.response(size=size) for size in (sizes if sizes is not None else [100,200]))
        values.extend([self.response([root+'/.releases/'+'a'*40+'/backend/app.py']),
                       self.response(size=300),self.response(size=50),
                       self.response(dict(stage='migrations',migration_step='backup',sqlite_errorname='SQLITE_IOERR_FSYNC',sqlite_errorcode=1034))])
        session.fixture_responses = values
        session.get.side_effect = values
        return session

    def test_reads_only_metadata_and_closes_database_response_without_reading_it(self):
        session=self.session()
        report=inspection.inspect(session)
        self.assertEqual(report['database_bytes'],300)
        self.assertEqual(report['measured_backup_bytes'],300)
        self.assertEqual(report['backup_file_count'],2)
        self.assertEqual(report['sqlite_errorname'],'SQLITE_IOERR_FSYNC')
        session.post.assert_not_called()
        session.delete.assert_not_called()
        session.fixture_responses[4].iter_content.assert_not_called()
        for call in session.get.call_args_list:
            self.assertTrue(call.kwargs['stream'])
            self.assertFalse(call.kwargs['allow_redirects'])
            self.assertEqual(call.kwargs['timeout'],(10,30))

    def test_bounds_backup_header_requests(self):
        root=inspection.deployment.ROOT.as_posix()
        session=self.session([root+f'/data/backups/{index}.db' for index in range(50)], [1]*20)
        report=inspection.inspect(session)
        self.assertEqual(report['backup_file_count'],50)
        self.assertEqual(report['backup_headers_checked'],20)
        self.assertEqual(session.get.call_count,25)

    def test_unknown_sizes_are_reported_as_unknown(self):
        report=inspection.inspect(self.session(sizes=[None,100]))
        self.assertEqual(report['backup_sizes_available'],1)
        self.assertEqual(report['measured_backup_bytes'],100)

    def test_rejects_untrusted_paths_before_requesting_them(self):
        for path in ('/etc/passwd','/home/GauravChhajer/StudentConnect/data/backups/../../.env'):
            session=MagicMock()
            session.get.return_value=self.response([path])
            with self.assertRaises(ValueError):
                inspection.inspect(session)
            self.assertEqual(session.get.call_count,1)

    def test_oversized_metadata_is_rejected(self):
        response=self.response()
        response.iter_content.return_value=[b'x'*(inspection.MAX_JSON_BYTES+1)]
        with self.assertRaises(ValueError):
            inspection.bounded_json(response)

    def test_errors_do_not_log_credentials_or_response_text(self):
        with patch.dict(inspection.os.environ,{'PYTHONANYWHERE_API_TOKEN':'synthetic-private-token'}), \
                patch.object(inspection.requests,'Session') as factory, \
                patch.object(inspection,'inspect',side_effect=ValueError('private provider response')), \
                self.assertLogs(inspection.logger,level='ERROR') as logs:
            self.assertEqual(inspection.main(),1)
        output='\n'.join(logs.output)
        self.assertNotIn('synthetic-private-token',output)
        self.assertNotIn('private provider response',output)


if __name__=='__main__':
    unittest.main()
