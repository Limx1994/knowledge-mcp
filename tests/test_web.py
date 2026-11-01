import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

from config import load_config
import crashlog
from knowledge_store import KnowledgeStore, StoreError
from runtime import ServiceState
from web_admin import WebAdmin

TEMP = Path(__file__).resolve().parents[1] / '.test-work'
TEMP.mkdir(exist_ok=True)


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEMP)
        self.root = Path(self.temp.name)
        self.store = KnowledgeStore(self.root)
        self.web = WebAdmin(ServiceState(self.store))
        self.client = TestClient(self.web.app, base_url=self.web.base_url)
        self.origin = {'Origin': self.web.base_url}
        ticket = self.web.open_url().split('#ticket=')[1]
        response = self.client.post('/api/session', json={'ticket': ticket}, headers=self.origin)
        self.assertEqual(response.status_code, 200, response.text)
        self.headers = dict(self.origin, **{'X-CSRF-Token': response.json()['csrf']})
        self.assertIn('HttpOnly', response.headers['set-cookie'])
        self.assertIn('SameSite=strict', response.headers['set-cookie'])

    def tearDown(self):
        self.client.close()
        self.web.close()
        self.store.close()
        self.temp.cleanup()

    def post(self, endpoint, **body):
        response = self.client.post(endpoint, json=body, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_full_document_and_lifecycle(self):
        content = '\n'.join('资料行' for _ in range(1500))
        created = self.post('/api/documents', action='create', path='项目 A/记录.md', content=content)
        read = self.client.get('/api/documents', params={'path': created['path']}).json()
        self.assertEqual(read['content'], content)
        self.assertEqual(len(self.store.read_document(created['path'])['lines']), 200)
        updated = self.post('/api/documents', action='update', path=created['path'], content='网页修改知识库', expected_version=read['version'])
        self.assertEqual(self.store.search_documents('知识库')['total'], 1)
        self.store.update_document(created['path'], 'MCP 修改', updated['version'])
        conflict = self.client.post('/api/documents', json=dict(action='update', path=created['path'], content='过期修改', expected_version=updated['version']), headers=self.headers)
        self.assertEqual(conflict.status_code, 409)
        self.assertIn('版本冲突', conflict.json()['error'])
        current = self.store.read_full_document(created['path'])
        self.post('/api/documents', action='delete', path=created['path'], expected_version=current['version'])
        records = self.client.get('/api/trash').json()['items']
        self.post('/api/trash', action='restore', record_id=records[0]['record_id'])
        self.assertEqual(self.store.read_full_document(created['path'])['content'], 'MCP 修改')

    def test_gzip_and_line_chunk(self):
        content = '\n'.join(f'第 {i} 行 知识库' for i in range(60000))
        self.post('/api/documents', action='create', path='压缩.md', content=content)
        response = self.client.get('/api/documents', params={'path': '压缩.md'}, headers={'Accept-Encoding': 'gzip'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get('content-encoding'), 'gzip')
        self.assertGreater(len(response.content), 1024)

        line = '这是一行用于分段测试的正文，包含关键词 索引 与 知识库。\n'
        with (self.store.docs / '大文件.md').open('w', encoding='utf-8') as stream:
            for _ in range(140000):
                stream.write(line)
        self.store.refresh_index()
        response = self.client.get('/api/documents', params={'path': '大文件.md', 'auto': 'true', 'line': 100000})
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data['start_line'], 100000)
        self.assertIn('start_byte', data)
        self.assertTrue(data['content'].startswith('这是一行用于分段测试'))

    def test_directory_versions_and_moves(self):
        self.post('/api/tree', action='create', path='空文件夹/子目录')
        result = self.client.get('/api/tree').json()
        folder = result['tree']['children'][0]
        self.assertEqual(folder['files'], 0)
        child = folder['children'][0]
        self.post('/api/tree', action='delete', path=child['path'], expected_version=child['version'])
        self.store.create_document('空文件夹/笔记.md', '移动索引')
        with self.assertRaises(StoreError):
            self.store.move_entry(folder['path'], '另一个目录', folder['version'])
        folder = self.store.get_tree()['tree']['children'][0]
        self.post('/api/tree', action='move', path=folder['path'], target='项目/资料', expected_version=folder['version'])
        self.assertEqual(self.store.search_documents('移动索引')['items'][0]['path'], '项目/资料/笔记.md')
        with self.assertRaises(StoreError):
            self.store.move_entry('项目', '项目/内部', self.store.get_tree()['tree']['children'][0]['version'])
        with self.assertRaises(StoreError):
            self.store.delete_directory('项目', self.store.get_tree()['tree']['children'][0]['version'])

    @unittest.skipUnless(os.name == 'nt', 'Windows case-only rename')
    def test_case_only_rename(self):
        item = self.post('/api/documents', action='create', path='folder/readme.md', content='rename')
        body = dict(action='move', path=item['path'], target='folder/README.md', expected_version='stale')
        self.assertEqual(self.client.post('/api/documents', json=body, headers=self.headers).status_code, 409)
        body['expected_version'] = item['version']
        self.post('/api/documents', **body)
        folder = self.client.get('/api/tree').json()['tree']['children'][0]
        self.post('/api/tree', action='move', path='folder', target='Folder', expected_version=folder['version'])
        document = self.client.get('/api/documents').json()['items'][0]
        self.assertEqual(document['path'], 'Folder/README.md')
        self.assertEqual([p.name for p in self.store.docs.iterdir()], ['Folder'])
        self.assertEqual([p.name for p in (self.store.docs / 'Folder').iterdir()], ['README.md'])

    def test_security_and_assets(self):
        self.assertEqual(self.client.get('/api/status', headers={'Host': 'evil.test'}).status_code, 403)
        self.assertEqual(self.client.get('/api/status', headers={'Origin': 'http://evil.test'}).status_code, 403)
        result = self.client.post('/api/documents', json=dict(action='create', path='a.md'), headers=self.origin)
        self.assertEqual(result.status_code, 403)
        self.assertEqual(self.client.post('/api/documents', json=dict(action='create', path='a.md'), headers={'X-CSRF-Token': self.headers['X-CSRF-Token']}).status_code, 403)
        self.assertEqual(self.client.get('/api/documents', params={'path': '../server.py'}).status_code, 409)
        ticket = self.web.open_url().split('#ticket=')[1]
        self.assertEqual(self.client.post('/api/session', json={'ticket': ticket}, headers=self.origin).status_code, 200)
        self.assertEqual(self.client.post('/api/session', json={'ticket': ticket}, headers=self.origin).status_code, 403)
        for url in ['/', '/assets/app.js', '/assets/style.css', '/assets/book.svg']:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
            self.assertIn("default-src 'self'", response.headers['content-security-policy'])
        self.assertEqual(self.client.get('/assets/server.py').status_code, 404)
        self.client.cookies.clear()
        self.assertEqual(self.client.get('/api/status').status_code, 403)

    def test_index_job_and_report(self):
        self.store.create_document('a.md', '索引测试')
        response = self.client.post('/api/index', json={'full': True}, headers=self.headers)
        self.assertEqual(response.status_code, 202)
        deadline = time.monotonic() + 5
        while True:
            result = self.client.get('/api/index').json()
            if result['state'] != 'running':
                break
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        self.assertEqual(result['state'], 'completed')
        self.assertTrue(result['report']['ok'])
        self.assertIsNotNone(self.client.get('/api/status').json()['last_sync'])
        (self.store.docs / 'bad.md').write_bytes(b'\xff')
        response = self.client.get('/api/documents')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['total'], 1)
        self.assertEqual(response.json()['sync']['skipped'], 1)

    def test_crash_records_api(self):
        self.assertEqual(self.client.get('/api/crashes').json()['items'], [])
        try:
            raise ValueError('boom')
        except ValueError as exc:
            crashlog.record_exception('bridge', exc, root=self.root)
        listed = self.client.get('/api/crashes').json()
        self.assertEqual(listed['dir'], str(crashlog.crash_folder(self.root)))
        self.assertEqual(len(listed['items']), 1)
        self.assertIn('ValueError: boom', listed['items'][0]['error'])
        self.assertIn('Traceback (most recent call last)', listed['items'][0]['text'])
        denied = self.client.post('/api/crashes', json={'action': 'clear'})
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(self.client.post('/api/crashes', json={'action': 'list'}, headers=self.headers).status_code, 400)
        self.assertEqual(self.post('/api/crashes', action='clear')['removed'], 1)
        self.assertEqual(self.client.get('/api/crashes').json()['items'], [])

    def test_file_permission_error_is_503(self):
        created = self.post('/api/documents', action='create', path='a.md', content='x')
        with patch.object(self.store, 'update_document', side_effect=PermissionError('locked')):
            response = self.client.post('/api/documents', json=dict(
                action='update', path='a.md', content='y',
                expected_version=created['version']), headers=self.headers)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(str(self.root), response.json()['error'])

    def test_chunk_api_and_conflict(self):
        file = self.store.docs / 'large.md'
        with file.open('wb') as stream:
            for _ in range(11):
                stream.write(b'x' * 1048576)
        read = self.client.get('/api/documents', params={'path': 'large.md', 'auto': 'true'}).json()
        self.assertEqual(read['start_byte'], 0)
        self.assertEqual(read['total_bytes'], 11 * 1048576)
        self.assertEqual(len(read['content']), 262144)
        updated = self.post('/api/documents', action='patch', path='large.md', start_byte=0,
                            end_byte=8, content='modified', expected_version=read['version'])
        self.assertEqual(self.store.search_documents('modified')['total'], 1)
        response = self.client.post('/api/documents', json=dict(action='patch', path='large.md',
                                    start_byte=0, end_byte=8, content='bad', expected_version=read['version']), headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertIn('版本冲突', response.json()['error'])
        tail = self.client.get('/api/documents', params={'path': 'large.md', 'offset': 11 * 1048576 - 8}).json()
        self.assertFalse(tail['has_more'])
        self.assertEqual(tail['version'], updated['version'])
        self.assertEqual(self.client.get('/api/documents', params={'path': 'large.md'}).status_code, 409)


if __name__ == '__main__':
    unittest.main()


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEMP)
        self.folder = Path(self.temp.name)
        self.config = self.folder / 'config.json'
        self.env = patch.dict(os.environ, {'KNOWLEDGE_MCP_CONFIG': str(self.config)})
        self.env.start()
        self.state = ServiceState()
        self.web = WebAdmin(self.state)
        self.client = TestClient(self.web.app, base_url=self.web.base_url)
        self.origin = {'Origin': self.web.base_url}
        ticket = self.web.open_url().split('#ticket=')[1]
        response = self.client.post('/api/session', json={'ticket': ticket}, headers=self.origin)
        self.assertEqual(response.status_code, 200, response.text)
        self.headers = dict(self.origin, **{'X-CSRF-Token': response.json()['csrf']})

    def tearDown(self):
        if self.state.store is not None:
            self.state.store.close()
        self.client.close()
        self.web.close()
        self.env.stop()
        self.temp.cleanup()

    def test_setup_persists_and_unlocks_api(self):
        status = self.client.get('/api/status').json()
        self.assertFalse(status['configured'])
        self.assertEqual(status['config_path'], str(self.config))
        self.assertEqual(self.client.get('/api/documents').status_code, 409)
        kb = self.folder / '知识库'
        response = self.client.post('/api/setup', json={'root': str(kb), 'create': True}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['root'], str(kb.resolve()))
        self.assertEqual(load_config()['root'], str(kb.resolve()))
        status = self.client.get('/api/status').json()
        self.assertTrue(status['configured'])
        self.assertEqual(status['root'], str(kb.resolve()))
        created = self.client.post('/api/documents', json={'action': 'create', 'path': 'a.md', 'content': '知识库'}, headers=self.headers)
        self.assertEqual(created.status_code, 200, created.text)
        again = self.client.post('/api/setup', json={'root': str(kb)}, headers=self.headers)
        self.assertEqual(again.status_code, 409)

    def test_setup_validation(self):
        missing = self.folder / '不存在'
        response = self.client.post('/api/setup', json={'root': str(missing)}, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertFalse(missing.exists())
        response = self.client.post('/api/setup', json={'root': 'relative'}, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        file = self.folder / 'file.txt'
        file.write_text('x', encoding='utf-8')
        response = self.client.post('/api/setup', json={'root': str(file)}, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertIsNone(self.state.store)


if __name__ == '__main__':
    unittest.main()
