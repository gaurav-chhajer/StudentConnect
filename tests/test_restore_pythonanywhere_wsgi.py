"""Offline emergency restore checks."""

import os
import unittest
from unittest.mock import MagicMock, patch

from scripts import restore_pythonanywhere_wsgi as restore


class RestoreTests(unittest.TestCase):
    def test_uploads_known_good_entry_before_reload(self):
        session = MagicMock()
        session.__enter__.return_value = session
        session.post.return_value.__enter__.return_value.status_code = 200
        with patch.dict(os.environ, {'PYTHONANYWHERE_API_TOKEN': 'synthetic-token'}), \
                patch.object(restore.subprocess, 'run') as show, \
                patch.object(restore.requests, 'Session', return_value=session):
            show.return_value.stdout = b'known good entry'
            restore.restore()
        self.assertEqual(session.post.call_count, 2)
        self.assertIn('wsgi.py', session.post.call_args_list[0].args[0])
        self.assertIn('/reload/', session.post.call_args_list[1].args[0])
        self.assertEqual(show.call_args.args[0][1], 'show')

    def test_rejected_upload_never_reloads(self):
        session = MagicMock()
        session.__enter__.return_value = session
        session.post.return_value.__enter__.return_value.status_code = 403
        with patch.dict(os.environ, {'PYTHONANYWHERE_API_TOKEN': 'synthetic-token'}), \
                patch.object(restore.subprocess, 'run') as show, \
                patch.object(restore.requests, 'Session', return_value=session):
            show.return_value.stdout = b'known good entry'
            with self.assertRaisesRegex(RuntimeError, 'HTTP 403'):
                restore.restore()
        self.assertEqual(session.post.call_count, 1)


if __name__ == '__main__':
    unittest.main()
