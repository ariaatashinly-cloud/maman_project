import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

# The module exposes a production ASGI app; tests supply inert placeholders.
os.environ.update({
    'TELEGRAM_BOT_TOKEN': 'test:token', 'TELEGRAM_OWNER_ID': '12345',
    'GOOGLE_CLIENT_ID': 'test-id', 'GOOGLE_CLIENT_SECRET': 'test-secret',
    'PUBLIC_BASE_URL': 'https://localhost.test',
    'ADMIN_PASSWORD': 'a-long-test-password', 'SESSION_SECRET': 'x' * 48,
    'DATA_DIR': tempfile.mkdtemp(prefix='drivebridge-import-'),
})
from app.main import Config, create_app, safe_name  # noqa: E402


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(
            token='test:token', owner=12345, api_url='http://127.0.0.1:8081',
            telegram_root='/missing', client_id='test-id', client_secret='test-secret',
            public_url='https://localhost.test', admin_password='a-long-test-password',
            session_secret='y' * 48, data_dir=self.temp.name, secure_cookie=False,
        )
        self.app = create_app(self.config)
        self.client = TestClient(self.app)
        self.app.state.drive.connected = lambda: True

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def login(self):
        result = self.client.post('/api/login', json={'password': self.config.admin_password})
        self.assertEqual(200, result.status_code)
        return self.client.get('/api/me').json()['csrf']

    def test_private_access_and_chunk_integrity(self):
        self.assertEqual(401, self.client.get('/api/jobs').status_code)
        self.assertEqual(401, self.client.post('/api/login', json={'password': 'wrong'}).status_code)
        csrf = self.login()
        self.assertEqual(403, self.client.post('/api/uploads', json={'name': 'a.mp4', 'size': 5}).status_code)
        headers = {'X-CSRF-Token': csrf}
        upload = self.client.post('/api/uploads', headers=headers,
                                  json={'name': '../demo.mp4', 'size': 5, 'mime': 'video/mp4'})
        self.assertEqual(200, upload.status_code)
        item_id = upload.json()['id']
        self.assertEqual(409, self.client.put('/api/uploads/' + item_id + '?offset=1',
                                              headers=headers, content=b'abc').status_code)
        self.assertEqual(409, self.client.post('/api/uploads/' + item_id + '/finish', headers=headers).status_code)
        self.assertEqual(200, self.client.put('/api/uploads/' + item_id + '?offset=0',
                                              headers=headers, content=b'abc').status_code)
        self.assertEqual(409, self.client.put('/api/uploads/' + item_id + '?offset=0',
                                              headers=headers, content=b'zz').status_code)
        self.assertEqual(200, self.client.put('/api/uploads/' + item_id + '?offset=3',
                                              headers=headers, content=b'de').status_code)
        self.assertEqual(200, self.client.post('/api/uploads/' + item_id + '/finish', headers=headers).status_code)
        job = self.app.state.store.one('jobs', item_id)
        self.assertEqual('demo.mp4', job['name'])
        self.assertEqual(b'abcde', Path(job['source']).read_bytes())
        self.assertNotIn('source', self.client.get('/api/jobs').json()['files'][0])
        self.assertIsNone(job['permission_id'])

    def test_share_revoke_and_trash(self):
        csrf = self.login()
        headers = {'X-CSRF-Token': csrf}
        source = Path(self.temp.name) / 'source.bin'
        source.write_bytes(b'abc')
        store = self.app.state.store
        store.enqueue('j1', 'clip.mp4', 'video/mp4', 3, str(source), 'browser')
        store.update_job('j1', status='ready', drive_id='g1', drive_url='https://drive.google.com/file/d/g1/view')
        calls = []
        self.app.state.drive.share = lambda file_id: (calls.append(('share', file_id)) or 'p1')
        self.app.state.drive.unshare = lambda file_id, permission: calls.append(('unshare', permission))
        self.app.state.drive.trash = lambda file_id: calls.append(('trash', file_id))
        self.assertEqual(403, self.client.post('/api/jobs/j1/share', headers={'X-CSRF-Token': 'bad'}).status_code)
        self.assertEqual(200, self.client.post('/api/jobs/j1/share', headers=headers).status_code)
        self.assertEqual('p1', store.one('jobs', 'j1')['permission_id'])
        self.assertEqual(200, self.client.post('/api/jobs/j1/unshare', headers=headers).status_code)
        self.assertIsNone(store.one('jobs', 'j1')['permission_id'])
        self.assertEqual(200, self.client.post('/api/jobs/j1/trash', headers=headers).status_code)
        self.assertIsNone(store.one('jobs', 'j1'))
        self.assertFalse(source.exists())
        self.assertEqual([('share', 'g1'), ('unshare', 'p1'), ('trash', 'g1')], calls)

    def test_oauth_state_and_telegram_replay(self):
        csrf = self.login()
        self.assertEqual(400, self.client.get('/auth/google/callback?state=wrong&code=x').status_code)
        store = self.app.state.store
        store.enqueue('one', 'x', 'text/plain', 1, '/missing', 'telegram', 12345, 99)
        with self.assertRaises(Exception):
            store.enqueue('two', 'x', 'text/plain', 1, '/missing', 'telegram', 12345, 99)
        self.assertEqual('file', safe_name('\\..\\'))


if __name__ == '__main__':
    unittest.main()
