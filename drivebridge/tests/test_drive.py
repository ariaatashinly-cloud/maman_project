import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.google_drive import Drive


class Response(io.BytesIO):
    def __init__(self, payload=b'', status=200, headers=None):
        super().__init__(payload)
        self.status = status
        self.headers = headers or {}


class ResumableUploadTests(unittest.TestCase):
    def test_chunk_ranges_and_app_owned_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'video.mp4'
            path.write_bytes(b'a' * (256 * 1024) + b'z')
            drive = Drive(temp, 'id', 'secret', 'https://localhost.test')
            drive.access_token = lambda: 'fake-access-token'
            drive.ensure_folder = lambda: 'app-owned-folder'
            calls = []

            def fake_request(method, url, headers=None, body=None, timeout=120):
                calls.append((method, url, headers, body))
                if method == 'POST':
                    return Response(headers={'Location': 'https://www.googleapis.com/upload/session/test'})
                if len(calls) == 2:
                    return Response(status=308, headers={'Range': 'bytes=0-262143'})
                return Response(json.dumps({'id': 'drive-file', 'webViewLink': 'https://drive.google.com/x'}).encode())

            with patch('app.google_drive.CHUNK_SIZE', 256 * 1024), patch('app.google_drive.request', fake_request):
                result = drive.upload(path, 'video.mp4', 'video/mp4', path.stat().st_size, 'job-123')
            self.assertEqual('drive-file', result['id'])
            metadata = json.loads(calls[0][3])
            self.assertEqual(['app-owned-folder'], metadata['parents'])
            self.assertEqual('job-123', metadata['appProperties']['bridgeJobId'])
            self.assertEqual('bytes 0-262143/262145', calls[1][2]['Content-Range'])
            self.assertEqual('bytes 262144-262144/262145', calls[2][2]['Content-Range'])
            self.assertEqual(256 * 1024, len(calls[1][3]))
            self.assertEqual(b'z', calls[2][3])


if __name__ == '__main__':
    unittest.main()
