import codecs
import gc
import os
from pathlib import Path
import sqlite3
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch

from knowledge_store import KnowledgeStore, StoreError, FULL_BYTES

PROJECT = Path(__file__).resolve().parents[1]
TEMP = PROJECT / '.test-work'
TEMP.mkdir(exist_ok=True)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEMP)
        self.root = Path(self.temp.name)
        self.store = KnowledgeStore(self.root)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def add(self, path='开发规范/Python.md', content='知识库 Python 测试\n第二行'):
        return self.store.create_document(path, content)

    def test_search_and_folders(self):
        self.add()
        self.add('项目经验/A/Python.md', 'Python 经验')
        self.assertEqual(self.store.search_documents('知识')['total'], 1)
        self.assertEqual(self.store.search_documents('知识库 PYTHON')['total'], 1)
        self.assertEqual(self.store.search_documents('不存在')['total'], 0)
        self.assertEqual(self.store.search_documents('开发规范')['total'], 1)
        self.assertEqual(self.store.list_documents(recursive=False)['total'], 0)
        self.assertEqual(self.store.list_documents('开发规范', False)['total'], 1)
        self.assertEqual(self.store.list_documents(limit=1, offset=1)['items'][0]['path'], '项目经验/A/Python.md')

    def test_unicode_names_are_distinct(self):
        # NTFS keeps these apart; casefold would wrongly merge them into "strasse.md".
        self.add('Straße.md', 'first note')
        self.add('Strasse.md', 'second note')
        self.assertEqual(self.store.list_documents()['total'], 2)
        self.assertEqual(self.store.read_full_document('Straße.md')['content'], 'first note')
        self.assertEqual(self.store.read_full_document('Strasse.md')['content'], 'second note')
        self.assertEqual(self.store.search_documents('second')['total'], 1)
        self.assertEqual(self.store.move_entry('Straße.md', '迁移/Straße.md',
                                               self.store.read_full_document('Straße.md')['version'])['ok'], True)
        self.assertEqual(self.store.list_documents('迁移')['total'], 1)

    def test_search_streaming_memory_and_paging(self):
        padding = ('x' * 8191 + '\n') * 64
        expected = []
        for number in range(20):
            path = f'{number:02}.md'
            score = number % 3 + 1
            (self.store.docs / path).write_text('needle\n' * score + padding, encoding='utf-8')
            expected.append((path, score))
        self.assertTrue(self.store.refresh_index()['ok'])
        expected.sort(key=lambda item: (-item[1], item[0]))
        for query in ['needle', 'ne']:
            with self.subTest(query=query):
                tracemalloc.start()
                try:
                    page = self.store.search_documents(query, offset=3, limit=4)
                    peak = tracemalloc.get_traced_memory()[1]
                finally:
                    tracemalloc.stop()
                self.assertLess(peak, 6 * 1024 * 1024)
                self.assertEqual(page['total'], 20)
                self.assertEqual([(r['path'], r['score']) for r in page['items']], expected[3:7])
                self.assertTrue(all(r['snippets'][0]['line'] == 1 for r in page['items']))

    def test_search_fts_candidates(self):
        files = {
            '稀有关键词/path.md': 'unrelated',
            'match.md': '稀有关键词 common_unique_suffix a"b+c\n',
            'false.md': 'common_prefix 稀有词 关键词 uni\n',
            'other.md': 'nothing here',
            'selected/nested.md': '稀有关键词\n',
            'unicode.md': 'İabc Σίσυφος\n',
        }
        for path, content in files.items():
            file = self.store.docs / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(content, encoding='utf-8')
        self.assertTrue(self.store.refresh_index()['ok'])
        # Query count must depend on distinct grams, not document count.
        statements = []
        self.store.conn.set_trace_callback(statements.append)
        try:
            found = self.store.search_documents('稀有关键词')
        finally:
            self.store.conn.set_trace_callback(None)
        self.assertEqual([r['path'] for r in found['items']],
                         ['match.md', 'selected/nested.md', '稀有关键词/path.md'])
        matches = [sql for sql in statements if 'chunk_search MATCH' in sql]
        self.assertGreater(len(matches), 0)
        self.assertLessEqual(len(matches), 3)
        self.assertEqual(self.store.search_documents('common_unique_suffix')['total'], 1)
        self.assertEqual(self.store.search_documents('a"b+c')['total'], 1)
        self.assertEqual(self.store.search_documents('i\u0307abc')['total'], 1)
        self.assertEqual(self.store.search_documents('σίσυφος')['total'], 1)
        self.assertEqual(self.store.search_documents('稀有关键词', 'selected', False)['total'], 1)
        self.assertEqual(self.store.search_documents('稀有关键词 common_unique_suffix')['total'], 1)
        self.assertEqual(self.store.search_documents('稀有关键词', offset=2, limit=1)['items'][0]['score'], 0)
        # First/middle/last grams may lie in different chunks of one match.
        cross = self.store.docs / 'cross.md'
        cross.write_bytes(b'x' * (1024 * 1024 - 4) + b'abcdefghijk\n')
        self.assertEqual(self.store.search_documents('abcdefghijk')['total'], 1)
        cross.write_bytes(b'x' * (1024 * 1024 - 3) + '稀有关键词\n'.encode())
        self.assertEqual(self.store.search_documents('稀有关键词')['total'], 4)

    @unittest.skipUnless(os.name == 'nt', 'Windows case-only rename')
    def test_case_only_rename_guards(self):
        item = self.add('folder/readme.md', 'case rename')
        with self.assertRaisesRegex(StoreError, '版本冲突'):
            self.store.move_entry('folder/readme.md', 'folder/README.md', 'stale')
        moved = self.store.move_entry('folder/readme.md', 'folder/README.md', item['version'])
        self.assertEqual(moved['path'], 'folder/README.md')
        self.assertEqual([p.name for p in (self.store.docs / 'folder').iterdir()], ['README.md'])
        folder = self.store.get_tree()['tree']['children'][0]
        with self.assertRaisesRegex(StoreError, '版本冲突'):
            self.store.move_entry('folder', 'Folder', 'stale')
        self.store.move_entry('folder', 'Folder', folder['version'])
        self.assertEqual([p.name for p in self.store.docs.iterdir()], ['Folder'])
        self.assertEqual(self.store.search_documents('case')['items'][0]['path'], 'Folder/README.md')
        for target in ['Folder', 'Folder/child']:
            with self.assertRaisesRegex(StoreError, '自身或子目录'):
                self.store.move_entry('Folder', target, folder['version'])
        self.add('occupied.md', 'keep')
        with self.assertRaises(FileExistsError):
            self.store.move_entry('Folder/README.md', 'occupied.md', item['version'])
        self.store.create_directory('occupied')
        with self.assertRaises(FileExistsError):
            self.store.move_entry('Folder', 'occupied', folder['version'])
        self.assertEqual(self.store.read_full_document('occupied.md')['content'], 'keep')

    def test_lifecycle(self):
        item = self.add()
        read = self.store.read_document(item['path'], max_lines=1)
        self.assertTrue(read['has_more'])
        self.assertEqual(read['version'], item['version'])
        updated = self.store.update_document(item['path'], '新资料', item['version'])
        with self.assertRaises(StoreError):
            self.store.delete_document(item['path'], item['version'])
        deleted = self.store.delete_document(item['path'], updated['version'])
        self.assertEqual(self.store.search_documents('新资料')['total'], 0)
        self.assertEqual(self.store.list_deleted()['total'], 1)
        self.store.restore_document(deleted['record_id'])
        self.assertEqual(self.store.search_documents('新资料')['total'], 1)
        self.assertEqual(self.store.list_deleted()['total'], 0)

    def test_external_change_and_rename(self):
        self.add()
        folder = self.store.docs / '开发规范'
        (folder / 'Python.md').write_text('外部修改', encoding='utf-8')
        self.assertEqual(self.store.search_documents('外部修改')['total'], 1)
        folder.rename(self.store.docs / '新目录')
        rows = self.store.list_documents()['items']
        self.assertEqual(rows[0]['path'], '新目录/Python.md')
        (self.store.docs / rows[0]['path']).unlink()
        self.assertEqual(self.store.list_documents()['total'], 0)

    def test_incremental_and_full(self):
        self.add('a.md', 'abc')
        with patch.object(self.store, '_load', side_effect=AssertionError('不得重复解析')):
            self.assertEqual(self.store.refresh_index()['unchanged'], 1)
        file = self.store.docs / 'a.md'
        before = file.stat()
        file.write_bytes(b'xyz')
        os.utime(file, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(self.store.search_documents('abc')['total'], 1)
        self.assertTrue(self.store.refresh_index(full=True)['ok'])
        self.assertEqual(self.store.search_documents('xyz')['total'], 1)
        first = self.store.list_documents()['items']
        self.store.refresh_index(full=True)
        self.assertEqual(self.store.list_documents()['items'], first)

    def test_metadata_queries_skip_content(self):
        self.add('a.md', 'large document\n' * 80000)
        self.add('b.md', 'second')
        denied = []

        def authorize(action, table, column, database, source):
            if action == sqlite3.SQLITE_READ and table == 'chunks' and column == 'content':
                denied.append(column)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        self.store.conn.set_authorizer(authorize)
        try:
            with patch.object(self.store, '_load', side_effect=AssertionError('不得重复读取')):
                self.assertEqual(self.store.refresh_index()['unchanged'], 2)
                page = self.store.list_documents(offset=1, limit=1)
            self.assertEqual(page['total'], 2)
            self.assertEqual([r['path'] for r in page['items']], ['b.md'])
            self.assertNotIn('content', page['items'][0])
            self.assertEqual(denied, [])
        finally:
            self.store.conn.set_authorizer(None)

    def test_scan_releases_content_without_gc(self):
        (self.store.docs / 'large.md').write_bytes(b'abc\n' * 524288)
        enabled = gc.isenabled()
        gc.disable()
        tracemalloc.start()
        try:
            self.assertEqual(self.store.refresh_index()['added'], 1)
            current, peak = tracemalloc.get_traced_memory()
            self.assertGreater(peak, 2 * 1024 * 1024)
            self.assertLess(current, 512 * 1024)
            self.assertEqual(self.store.refresh_index(full=True)['updated'], 1)
            self.assertLess(tracemalloc.get_traced_memory()[0], 512 * 1024)
        finally:
            tracemalloc.stop()
            if enabled:
                gc.enable()

    def test_directory_sql_paging(self):
        paths = ['资料%_!/A.md', '资料%_!/b.md', '资料%_!/子目录/c.md',
                 '资料XY!/leak.md', 'Folder/a.md', 'Folder/nested/b.md', 'ß/a.md']
        for path in paths:
            file = self.store.docs / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text('needle', encoding='utf-8')
        page = self.store.list_documents('资料%_!', False, offset=1, limit=1)
        self.assertEqual(page['total'], 2)
        self.assertEqual([r['path'] for r in page['items']], ['资料%_!/b.md'])
        self.assertEqual(self.store.list_documents('资料%_!')['total'], 3)
        self.assertEqual(self.store.list_documents('资料%_!', offset=3)['items'], [])
        self.assertEqual(self.store.list_documents('folder', False)['total'], 1)
        self.assertEqual(self.store.list_documents('SS')['total'], 0)
        self.assertEqual(self.store.list_documents('ß')['total'], 1)
        self.assertEqual(self.store.list_documents(recursive=False)['total'], 0)
        found = self.store.search_documents('needle', '资料%_!', False, offset=1, limit=1)
        self.assertEqual(found['total'], 2)
        self.assertEqual(found['items'][0]['path'], '资料%_!/b.md')

    def test_invalid_paths(self):
        for path in ['../a.md', '/a.md', 'C:/a.md', 'C:a.md', 'a/../b.md', 'a//b.md',
                     'NUL.md', 'COM1.txt', 'a./b.md', 'a:stream.md', 'a.pdf', 'a\x00.md']:
            with self.subTest(path=path), self.assertRaises(StoreError):
                self.add(path)

    def test_conflicts(self):
        item = self.add('a.md')
        with self.assertRaises(FileExistsError):
            self.add('a.md')
        with self.assertRaises(StoreError):
            self.store.update_document('a.md', 'overwrite', 'stale')
        deleted = self.store.delete_document('a.md', item['version'])
        self.add('a.md', '占用')
        with self.assertRaises(FileExistsError):
            self.store.restore_document(deleted['record_id'])
        self.assertEqual(self.store.list_deleted()['total'], 1)

    def test_bom_and_newlines(self):
        file = self.store.docs / 'bom.TXT'
        file.write_bytes(codecs.BOM_UTF8 + b'a\r\nb\r\n')
        item = self.store.read_document('bom.TXT')
        self.store.update_document('bom.TXT', 'c\nd\n', item['version'])
        self.assertEqual(file.read_bytes(), codecs.BOM_UTF8 + b'c\r\nd\r\n')

    def test_scan_failure_skips_bad_file(self):
        self.add('a.md')
        (self.store.docs / 'bad.md').write_bytes(b'\xff')
        report = self.store.refresh_index()
        self.assertTrue(report['ok'])
        self.assertEqual(report['skipped'], 1)
        self.assertEqual(report['failed'], 0)
        self.assertEqual(self.store.search_documents('Python')['total'], 1)
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM documents').fetchone()[0], 1)

    def test_restore_cleanup_failure_is_not_fatal(self):
        item = self.add('a.md', 'restore me')
        deleted = self.store.delete_document('a.md', item['version'])
        real_unlink = Path.unlink

        def fail_content(path, *args, **kwargs):
            if path.name == 'content':
                raise OSError('locked')
            return real_unlink(path, *args, **kwargs)

        with patch.object(Path, 'unlink', fail_content):
            result = self.store.restore_document(deleted['record_id'])
            self.assertTrue(result['ok'])
            self.assertEqual(self.store.read_full_document('a.md')['content'], 'restore me')
            self.assertEqual(self.store.list_deleted()['total'], 0)

    def test_purge_document(self):
        item = self.add('a.md', 'purge me')
        deleted = self.store.delete_document('a.md', item['version'])
        result = self.store.purge_document(deleted['record_id'])
        self.assertTrue(result['ok'])
        self.assertEqual(result['path'], 'a.md')
        self.assertEqual(self.store.list_deleted()['total'], 0)
        self.assertFalse((self.store.trash / deleted['record_id']).exists())
        with self.assertRaises(StoreError):
            self.store.purge_document(deleted['record_id'])
        with self.assertRaises(StoreError):
            self.store.purge_document('zz')

    def test_purge_all_deleted(self):
        for name in ('a.md', 'b.md', 'c.md'):
            item = self.add(name, name)
            self.store.delete_document(name, item['version'])
        result = self.store.purge_all_deleted()
        self.assertTrue(result['ok'])
        self.assertEqual(result['removed'], 3)
        self.assertEqual(self.store.list_deleted()['total'], 0)
        self.assertEqual(self.store.purge_all_deleted()['removed'], 0)

    def test_purge_cleanup_failure_raises(self):
        item = self.add('a.md', 'keep me')
        deleted = self.store.delete_document('a.md', item['version'])
        real_unlink = Path.unlink

        def fail_content(path, *args, **kwargs):
            if path.name == 'content':
                raise OSError('locked')
            return real_unlink(path, *args, **kwargs)

        with patch.object(Path, 'unlink', fail_content):
            with self.assertRaises(StoreError):
                self.store.purge_document(deleted['record_id'])
        self.assertEqual(self.store.list_deleted()['total'], 1)

    def test_short_query_cross_chunk(self):
        (self.store.docs / 'edge.md').write_bytes(b'x' * (1024 * 1024 - 1) + b'ne\n')
        self.assertEqual(self.store.search_documents('ne')['total'], 1)

    def test_limits_and_skips(self):
        with self.assertRaises(StoreError):
            self.add('big.md', 'a' * (FULL_BYTES + 1))
        (self.store.docs / 'unsupported.pdf').write_bytes(b'pdf')
        report = self.store.refresh_index()
        self.assertEqual(report['skipped'], 1)
        self.assertTrue(report['issues'][0]['reason'])
        with self.assertRaises(StoreError):
            self.store.list_documents(limit=101)

    def test_permission_failure(self):
        self.add()
        with patch('knowledge_store.os.scandir', side_effect=PermissionError('denied')):
            report = self.store.refresh_index()
        self.assertFalse(report['ok'])
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM documents').fetchone()[0], 1)

    def test_database_lock_and_partial(self):
        self.add('a.md', 'old')
        other = sqlite3.connect(self.store.data / 'knowledge.db')
        other.execute('BEGIN EXCLUSIVE')
        try:
            with self.assertRaises((sqlite3.Error, StoreError)):
                self.store.list_documents()
        finally:
            other.rollback()
            other.close()
        with patch.object(self.store, '_index_one', side_effect=sqlite3.Error('locked')):
            with self.assertRaisesRegex(StoreError, '文件已修改、索引未同步'):
                self.add('b.md', 'new')
        self.assertEqual(self.store.search_documents('new')['total'], 1)

    def test_second_process_lock(self):
        with self.assertRaisesRegex(StoreError, '已有服务进程'):
            KnowledgeStore(self.root)

    def test_empty_and_quotes(self):
        self.assertEqual(self.store.list_documents()['total'], 0)
        with self.assertRaises(StoreError):
            self.store.search_documents('  ')
        self.add('quote.md', 'a"b+c AND SQL 注入')
        self.assertEqual(self.store.search_documents('a"b+c')['total'], 1)

    def test_quoted_phrase_search(self):
        self.add('a.md', '知识库 索引\n另一行')
        self.add('b.md', '知识库\n索引')
        self.assertEqual(self.store.search_documents('"知识库 索引"')['total'], 1)
        self.assertEqual(self.store.search_documents('"知识库"')['total'], 2)
        self.assertEqual(self.store.search_documents('知识库 "索引"')['total'], 2)
        with self.assertRaises(StoreError):
            self.store.search_documents('""')

    def test_duplicate_create_is_friendly(self):
        self.add('folder/a.md', 'x')
        with self.assertRaises(FileExistsError) as caught:
            self.add('folder/a.md', 'y')
        message = str(caught.exception)
        self.assertIn('目标已存在', message)
        self.assertIn('folder/a.md', message)
        self.assertNotIn(str(self.root), message)

    def test_read_document_seeks_without_scanning_prefix(self):
        lines = [f'第 {i} 行 ' + ('内容' * (i % 7)) for i in range(1, 60001)]
        self.add('big.md', '\n'.join(lines))
        full = self.store.read_full_document('big.md')['content'].split('\n')
        for start in (1, 2, 999, 40000, 59990, 60000):
            result = self.store.read_document('big.md', start_line=start, max_lines=17)
            self.assertEqual([row['text'] for row in result['lines']],
                             full[start - 1:start - 1 + 17], start)
            self.assertEqual(result['lines'][0]['line'], start)
            self.assertEqual(result['has_more'], start - 1 + 17 < len(full))

    def test_tree_versions_and_unsupported(self):
        item = self.add('a.md', 'one')
        before = self.store.get_tree()['tree']
        self.assertEqual(before['unsupported'], 0)
        self.store.update_document('a.md', 'two', item['version'])
        after = self.store.get_tree()['tree']
        self.assertNotEqual(before['version'], after['version'])
        (self.store.docs / '附件').mkdir()
        (self.store.docs / '附件' / '报告.pdf').write_bytes(b'pdf')
        tree = self.store.get_tree()['tree']
        folder = next(node for node in tree['children'] if node['name'] == '附件')
        self.assertEqual(folder['unsupported'], 1)
        self.assertEqual(folder['files'], 1)
        self.assertEqual(self.store.list_entries('附件')['unsupported'], 1)

    def test_sync_omits_issue_details(self):
        for index in range(3):
            (self.store.docs / f'跳过-{index}.pdf').write_bytes(b'x')
        sync = self.store.list_documents()['sync']
        self.assertEqual(sync['issues'], [])
        self.assertEqual(sync['issues_total'], 3)
        report = self.store.refresh_index()
        self.assertEqual(len(report['issues']), 3)
        self.assertFalse(report['issues_truncated'])

    @unittest.skipUnless(os.name == 'nt', 'Windows junction test')
    def test_junction(self):
        import subprocess
        target = self.root / 'outside'
        target.mkdir()
        link = self.store.docs / 'linked'
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        try:
            with self.assertRaises(StoreError):
                self.add('linked/a.md')
            report = self.store.refresh_index()
            self.assertTrue(report['ok'])
            self.assertEqual(report['skipped'], 1)
            self.assertEqual(self.store.list_documents()['total'], 0)
            self.assertTrue(any(i['kind'] == 'skipped' and '重解析点' in i['reason'] for i in report['issues']))
        finally:
            link.rmdir()


if __name__ == '__main__':
    unittest.main()
