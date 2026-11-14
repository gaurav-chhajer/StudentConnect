"""Read-only hosting metadata for failed deployments; never downloads database contents."""

import json
import logging
import os
from pathlib import PurePosixPath

import requests

from scripts import deploy_pythonanywhere as deployment

logger = logging.getLogger(__name__)
MAX_JSON_BYTES = 1024 * 1024
MAX_BACKUP_HEADERS = 20


def bounded_json(response):
    content = bytearray()
    for chunk in response.iter_content(16384):
        content.extend(chunk)
        if len(content) > MAX_JSON_BYTES:
            raise ValueError('Metadata response exceeds the inspection limit')
    return json.loads(content)


def inspect(session):
    """Use only GET requests, bounded directory listings and response headers."""
    def get(endpoint, **kwargs):
        return session.get(deployment.API + endpoint, timeout=(10, 30),
                           allow_redirects=False, stream=True, **kwargs)

    def listing(path):
        with get('files/tree/', params={'path': str(path)}) as response:
            if response.status_code == 404:
                return []
            response.raise_for_status()
            values = bounded_json(response)
        if not isinstance(values, list) or len(values) > 1000:
            raise ValueError('Invalid file listing')
        for value in values:
            if (not isinstance(value, str) or len(value) > 600 or
                    '..' in PurePosixPath(value).parts or '\\' in value or
                    not PurePosixPath(value).is_relative_to(path)):
                raise ValueError('File listing escapes the inspected directory')
        return values

    def file_size(path):
        # Stream mode and closing without iterating avoid reading private contents.
        with get('files/path' + str(path), headers={'Accept-Encoding': 'identity'}) as response:
            if response.status_code == 404:
                return None
            response.raise_for_status()
            length = response.headers.get('Content-Length')
            if length is None:
                return None
            if not length.isascii() or not length.isdecimal() or len(length) > 13:
                raise ValueError('Invalid file-size metadata')
            return int(length)

    root = PurePosixPath(deployment.ROOT.as_posix())
    backups = listing(root / 'data/backups')
    db_files = sorted(value for value in backups if value.endswith('.db'))
    selected = db_files[-MAX_BACKUP_HEADERS:]
    sizes = [file_size(PurePosixPath(value)) for value in selected]
    releases = listing(root / '.releases')
    release_ids = {PurePosixPath(value).relative_to(root / '.releases').parts[0]
                   for value in releases if PurePosixPath(value).relative_to(root / '.releases').parts}
    result = dict(database_bytes=file_size(root / 'data/jobs.db'), backup_file_count=len(db_files),
                  backup_listing_may_be_truncated=len(backups) == 1000,
                  backup_headers_checked=len(selected), backup_sizes_available=sum(size is not None for size in sizes),
                  measured_backup_bytes=sum(size for size in sizes if size is not None),
                  release_directories_observed=len(release_ids),
                  release_listing_may_be_truncated=len(releases) == 1000,
                  archive_bytes=file_size(root / 'data/deploy-release.zip'))
    with get('files/path' + str(root / 'data/deployment-status.json')) as response:
        if response.status_code == 200:
            status = bounded_json(response)
            if not isinstance(status, dict):
                raise ValueError('Invalid deployment status')
            for key in ('stage', 'migration_step', 'sqlite_errorname', 'sqlite_errorcode'):
                value = status.get(key)
                if key == 'sqlite_errorcode':
                    if value is not None and (type(value) is not int or not 1 <= value <= 65535):
                        raise ValueError('Invalid SQLite diagnostic')
                elif value is not None and (not isinstance(value, str) or len(value) > 40 or
                        not all(char.isascii() and (char.isalnum() or char == '_') for char in value)):
                    raise ValueError('Invalid deployment diagnostic')
                result[key] = value
        elif response.status_code != 404:
            response.raise_for_status()
    return result


def main():
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    token = os.environ.get('PYTHONANYWHERE_API_TOKEN', '').strip()
    if not token:
        logger.error('Read-only storage inspection requires the existing hosting API credential')
        return 1
    try:
        with requests.Session() as session:
            session.headers.update(Authorization='Token ' + token)
            result = inspect(session)
        logger.info('Read-only hosted storage metadata: %s', json.dumps(result, sort_keys=True))
        logger.info('Quota availability is not reported by the Files API; inspect the hosting dashboard to confirm per-account usage. No files were changed.')
        return 0
    except (requests.RequestException, ValueError, KeyError, TypeError) as error:
        logger.error('Read-only hosting inspection failed (%s); no files were changed', type(error).__name__)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
