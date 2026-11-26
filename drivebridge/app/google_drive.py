"""Drive OAuth and resumable uploads using only the Python standard library."""

import json
import os
import re
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

SCOPE = 'https://www.googleapis.com/auth/drive.file'
AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
API_URL = 'https://www.googleapis.com/drive/v3'
UPLOAD_URL = 'https://www.googleapis.com/upload/drive/v3/files'
CHUNK_SIZE = 8 * 1024 * 1024  # A multiple of Drive's 256 KiB chunk size.


class DriveError(Exception):
    pass


def request(method, url, headers=None, body=None, timeout=120):
    req = Request(url, data=body, headers=headers or {}, method=method)
    try:
        return urlopen(req, timeout=timeout)
    except HTTPError as exc:
        if exc.code == 308:
            return exc
        detail = exc.read(600).decode('utf-8', 'replace')
        raise DriveError(f'Google HTTP {exc.code}: {detail}') from exc
    except URLError as exc:
        raise DriveError(f'Network: {exc.reason}') from exc


class Drive:
    def __init__(self, data_dir, client_id, client_secret, public_base, folder_id=''):
        self.path = Path(data_dir) / 'google-token.json'
        self.folder_path = Path(data_dir) / 'drive-folder.json'
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = public_base + '/auth/google/callback'
        self.folder_id = folder_id
        self.lock = threading.RLock()

    def authorization_url(self, state):
        return AUTH_URL + '?' + urlencode({
            'client_id': self.client_id, 'redirect_uri': self.redirect_uri,
            'response_type': 'code', 'access_type': 'offline',
            'prompt': 'consent', 'scope': SCOPE, 'state': state,
        })

    def connected(self):
        try:
            return bool(self._read().get('refresh_token'))
        except (OSError, ValueError):
            return False

    def _read(self):
        return json.loads(self.path.read_text())

    def _write(self, data):
        temp = self.path.with_suffix('.tmp')
        fd = os.open(temp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, self.path)

    def _exchange(self, payload):
        body = urlencode(payload).encode()
        with request('POST', TOKEN_URL, {'Content-Type': 'application/x-www-form-urlencoded'}, body) as res:
            return json.load(res)

    def connect(self, code):
        data = self._exchange({
            'code': code, 'client_id': self.client_id, 'client_secret': self.client_secret,
            'redirect_uri': self.redirect_uri, 'grant_type': 'authorization_code',
        })
        if not data.get('refresh_token'):
            raise DriveError('Google did not return a refresh token. Reconnect with consent.')
        with self.lock:
            self._write({
                'refresh_token': data['refresh_token'], 'access_token': data['access_token'],
                'expires_at': time.time() + data.get('expires_in', 3600),
            })

    def access_token(self):
        with self.lock:
            try:
                data = self._read()
            except (OSError, ValueError) as exc:
                raise DriveError('Connect Google Drive in the panel first.') from exc
            if data.get('access_token') and data.get('expires_at', 0) > time.time() + 90:
                return data['access_token']
            refreshed = self._exchange({
                'client_id': self.client_id, 'client_secret': self.client_secret,
                'refresh_token': data['refresh_token'], 'grant_type': 'refresh_token',
            })
            data.update(access_token=refreshed['access_token'],
                        expires_at=time.time() + refreshed.get('expires_in', 3600))
            self._write(data)
            return data['access_token']

    def json_api(self, method, path, body=None):
        headers = {'Authorization': 'Bearer ' + self.access_token()}
        if body is not None:
            headers['Content-Type'] = 'application/json; charset=utf-8'
        with request(method, API_URL + path, headers,
                     json.dumps(body).encode() if body is not None else None) as res:
            contents = res.read()
            return json.loads(contents) if contents else None

    def ensure_folder(self):
        if self.folder_id:
            return self.folder_id
        with self.lock:
            if self.folder_path.exists():
                return json.loads(self.folder_path.read_text())['id']
            folder = self.json_api('POST', '/files?fields=id', {
                'name': 'Telegram Drive Bridge',
                'mimeType': 'application/vnd.google-apps.folder',
            })
            temporary = self.folder_path.with_suffix('.tmp')
            temporary.write_text(json.dumps({'id': folder['id']}))
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.folder_path)
            return folder['id']

    def find_job_file(self, job_id):
        query = "trashed = false and appProperties has { key='bridgeJobId' and value='" + job_id + "' }"
        data = self.json_api('GET', '/files?' + urlencode({
            'q': query, 'fields': 'files(id,name,mimeType,size,webViewLink)', 'pageSize': 2,
        }))
        return (data.get('files') or [None])[0]

    def ensure_user_folder(self, telegram_id, label=None):
        root = self.ensure_folder()
        marker = str(int(telegram_id))
        query = ("trashed = false and mimeType = 'application/vnd.google-apps.folder' "
                 "and appProperties has { key='telegramUserId' and value='" + marker + "' }")
        data = self.json_api('GET', '/files?' + urlencode({
            'q': query, 'fields': 'files(id,name)', 'pageSize': 2,
        }))
        found = data.get('files') or []
        if found:
            return found[0]['id']
        clean = re.sub(r'[^\w.@ -]+', '_', str(label or '')).strip()[:60]
        folder = self.json_api('POST', '/files?fields=id', {
            'name': (clean + ' - ' if clean else '') + marker,
            'mimeType': 'application/vnd.google-apps.folder',
            'parents': [root],
            'appProperties': {'telegramUserId': marker},
        })
        return folder['id']

    def upload(self, path, name, mime, size, job_id, parent_id=None, progress=None):
        path = Path(path)
        if not path.is_file() or path.stat().st_size != size:
            raise DriveError('Source file is missing or changed. Please send it again.')
        metadata = {'name': name, 'parents': [parent_id or self.ensure_folder()],
                    'appProperties': {'bridgeJobId': job_id}}
        headers = {
            'Authorization': 'Bearer ' + self.access_token(),
            'Content-Type': 'application/json; charset=utf-8',
            'X-Upload-Content-Length': str(size),
            'X-Upload-Content-Type': mime,
        }
        url = UPLOAD_URL + '?uploadType=resumable&fields=id,name,mimeType,size,webViewLink'
        with request('POST', url, headers, json.dumps(metadata).encode()) as response:
            session_url = response.headers.get('Location')
        if not session_url or not session_url.startswith('https://www.googleapis.com/'):
            raise DriveError('Drive did not provide an upload session.')

        offset = 0
        consecutive_failures = 0
        with path.open('rb') as stream:
            while offset < size:
                before = offset
                stream.seek(offset)
                chunk = stream.read(min(CHUNK_SIZE, size - offset))
                if not chunk:
                    raise DriveError('File ended during upload.')
                end = offset + len(chunk) - 1
                put_headers = {
                    'Authorization': 'Bearer ' + self.access_token(),
                    'Content-Type': mime,
                    'Content-Length': str(len(chunk)),
                    'Content-Range': f'bytes {offset}-{end}/{size}',
                }
                for attempt in range(6):
                    try:
                        with request('PUT', session_url, put_headers, chunk, timeout=180) as response:
                            if response.status in (200, 201):
                                result = json.load(response)
                                if progress:
                                    progress(size, size)
                                return result
                            if response.status == 308:
                                acknowledged = response.headers.get('Range', '')
                                match = re.fullmatch(r'bytes=0-(\d+)', acknowledged)
                                offset = int(match.group(1)) + 1 if match else 0
                                break
                            raise DriveError(f'Unexpected upload status {response.status}')
                    except DriveError as exc:
                        if not any(f'HTTP {n}:' in str(exc) for n in (429, 500, 502, 503, 504)) and not str(exc).startswith('Network:'):
                            raise
                        consecutive_failures += 1
                        if consecutive_failures >= 6:
                            raise
                        time.sleep(min(2 ** attempt, 16))
                        offset = self._resume_offset(session_url, size)
                        break
                else:
                    raise DriveError('Upload failed repeatedly.')
                if offset > before:
                    consecutive_failures = 0
                    if progress:
                        progress(offset, size)
                else:
                    consecutive_failures += 1
                    if consecutive_failures >= 6:
                        raise DriveError('Drive upload stalled. Retry from the panel.')
        if size == 0:
            with request('PUT', session_url, {
                'Authorization': 'Bearer ' + self.access_token(),
                'Content-Length': '0', 'Content-Range': 'bytes */0',
            }, b'') as response:
                return json.load(response)
        raise DriveError('Upload ended without confirmation from Drive.')

    def _resume_offset(self, session_url, size):
        with request('PUT', session_url, {
            'Authorization': 'Bearer ' + self.access_token(),
            'Content-Length': '0', 'Content-Range': f'bytes */{size}',
        }, b'') as res:
            if res.status != 308:
                raise DriveError('Upload session has ended. Retry this file from the panel.')
            match = re.fullmatch(r'bytes=0-(\d+)', res.headers.get('Range', ''))
            return int(match.group(1)) + 1 if match else 0

    def rename(self, file_id, name):
        return self.json_api('PATCH', f'/files/{file_id}?fields=id,name,webViewLink', {'name': name})

    def trash(self, file_id):
        # Recoverable from the Google Drive trash.
        self.json_api('PATCH', f'/files/{file_id}', {'trashed': True})

    def share(self, file_id):
        return self.json_api('POST', f'/files/{file_id}/permissions?fields=id',
                             {'type': 'anyone', 'role': 'reader', 'allowFileDiscovery': False})['id']

    def unshare(self, file_id, permission_id):
        self.json_api('DELETE', f'/files/{file_id}/permissions/{permission_id}')
