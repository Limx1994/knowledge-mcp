import codecs
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import random
import unittest
from unittest.mock import patch

from knowledge_store import KnowledgeStore, StoreError
from document_stream import BLOCK_BYTES, inspect_file, search_chunks

TEMP = Path(__file__).resolve().parents[1] / '.test-work'
TEMP.mkdir(exist_ok=True)


class ChunkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEMP)
        self.root = Path(self.temp.name)
        self.store = KnowledgeStore(self.root)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_cross_block_search_and_lines(self):
        file = self.store.docs / 'cross.md'
        file.write_bytes(b'x' * (BLOCK_BYTES - 2) + '跨界needle\r\n'.encode() + b'lastword\n')
        found = self.store.search_documents('needle lastword 跨界')
        self.assertEqual(found['total'], 1)
        self.assertEqual(found['items'][0]['score'], 3)
        self.assertEqual(found['items'][0]['snippets'][0]['line'], 1)
        self.assertEqual(self.store.read_document('cross.md', 2)['lines'][0]['text'], 'lastword')
        # Self-overlapping patterns must retain str.count's non-overlap semantics.
        file.write_bytes(b'a' * (BLOCK_BYTES + 11))
        self.assertEqual(self.store.search_documents('aaa')['items'][0]['score'], (BLOCK_BYTES + 11) // 3)
        with self.assertRaisesRegex(StoreError, '分段|read_document_chunk'):
            self.store.read_document('cross.md')
        with file.open('ab') as stream:
            stream.write(b'\nselected\n')
        self.assertEqual(self.store.read_document('cross.md', 2)['lines'][0]['text'], 'selected')

    def test_search_count_boundaries(self):
        rng = random.Random(1729)
        for case in range(300):
            text = ''.join(rng.choices(['a', 'a', 'b', 'İ', '中', '\r\n'], k=900))
            words = ['a', 'aaa', 'aba', 'ababa', 'i\u0307', '中']
            rows = []
            offset = 0
            while offset < len(text):
                size = rng.randrange(1, 400)
                rows.append(dict(content=text[offset:offset + size]))
                offset += size
            counts, snippets = search_chunks(rows, words)
            with self.subTest(case=case):
                self.assertEqual(counts, [text.lower().count(word) for word in words])
                expected = [number for number, value in enumerate(text.splitlines(), 1)
                            if any(word in value.lower() for word in words)][:3]
                self.assertEqual([s['line'] for s in snippets], expected)
        # Retained context must not restart self-overlapping matches.
        for word in ['aa', 'aaa', 'aaaaa', 'aba', 'ababa']:
            text = 'a' * 1001 if word.startswith('aa') else 'ab' * 1001
            rows = [dict(content=text[i:i + 301]) for i in range(0, len(text), 301)]
            self.assertEqual(search_chunks(rows, [word])[0], [text.count(word)])
        word = 'a' * 50000
        text = 'a' * 400001
        rows = [dict(content=text[:200000]), dict(content=text[200000:])]
        self.assertEqual(search_chunks(rows, [word])[0], [text.count(word)])

    def test_search_snippet_order(self):
        text = 'early first\r\nsecond early\r\nthird late\r\nfourth late\r\n'
        counts, snippets = search_chunks([dict(content=text)], ['late', 'early', 'third'])
        self.assertEqual(counts, [2, 2, 1])
        self.assertEqual(snippets, [dict(line=1, text='early first'),
                                    dict(line=2, text='second early'),
                                    dict(line=3, text='third late')])
        counts, snippets = search_chunks([dict(content='İneedle\n' * 10000)], ['needle'])
        self.assertEqual(counts, [10000])
        self.assertEqual(snippets, [dict(line=n, text='İneedle') for n in [1, 2, 3]])

    def test_chunk_boundaries_and_patch(self):
        file = self.store.docs / 'bom.txt'
        original = codecs.BOM_UTF8 + ('中' * 10 + '\r\n末尾').encode()
        file.write_bytes(original)
        self.assertTrue(self.store.refresh_index()['ok'])
        offset = 0
        pieces = []
        while True:
            chunk = self.store.read_document_chunk('bom.txt', offset, 8)
            pieces.append(chunk['content'])
            self.assertFalse(chunk['content'].endswith('\r'))
            if not chunk['has_more']:
                break
            self.assertGreater(chunk['next_offset'], offset)
            offset = chunk['next_offset']
        self.assertEqual(''.join(pieces), original[3:].decode())
        aligned = self.store.read_document_chunk('bom.txt', 5, 8)
        self.assertEqual(aligned['start_byte'], 3)
        current = chunk['version']
        for start, end in [(0, 3), (4, 6), (33, 34), (100, 101), (8, 7)]:
            with self.subTest(start=start), self.assertRaises(StoreError):
                self.store.patch_document('bom.txt', start, end, 'x', current)
        updated = self.store.patch_document('bom.txt', 3, 6, '替换\n', current)
        self.assertEqual(file.read_bytes(), codecs.BOM_UTF8 + ('替换\r\n' + '中' * 9 + '\r\n末尾').encode())
        with self.assertRaisesRegex(StoreError, '版本冲突'):
            self.store.patch_document('bom.txt', 3, 3, 'x', current)
        self.assertEqual(inspect_file(file)['version'], updated['version'])

    def test_large_full_limit_and_invalid_utf8(self):
        file = self.store.docs / 'large.md'
        with file.open('wb') as stream:
            for _ in range(11):
                stream.write(b'x' * BLOCK_BYTES)
        self.assertTrue(self.store.refresh_index()['ok'])
        with self.assertRaisesRegex(StoreError, '10 MiB'):
            self.store.read_full_document('large.md')
        chunk = self.store.read_document_chunk('large.md')
        self.assertEqual(len(chunk['content']), 262144)
        with patch('document_stream.MAX_BYTES', file.stat().st_size), patch('knowledge_store.MAX_BYTES', file.stat().st_size):
            with self.assertRaisesRegex(StoreError, '1.5 GiB'):
                self.store.patch_document('large.md', 0, 0, 'x', chunk['version'])
        (self.store.docs / 'invalid.md').write_bytes(b'a\xe4\xb8')
        report = self.store.refresh_index()
        self.assertTrue(report['ok'])
        self.assertEqual(report['skipped'], 1)
        self.assertEqual(report['failed'], 0)
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM documents').fetchone()[0], 1)

    def test_directory_tools(self):
        self.store.create_directory('empty/sub')
        listing = self.store.list_entries('empty')
        self.assertEqual(listing['items'][0]['type'], 'directory')
        self.store.delete_directory('empty/sub', listing['items'][0]['version'])
        listing = self.store.list_entries('empty')
        self.store.rename_entry('empty', 'renamed', listing['version'], True)
        item = self.store.create_document('renamed/a.md', 'body')
        self.assertEqual(self.store.read_document('renamed\\a.md')['lines'][0]['text'], 'body')
        self.store.rename_entry('renamed/a.md', 'A.md', item['version'])
        self.store.move_typed('renamed/A.md', 'other/a.md', item['version'])
        with self.assertRaises(StoreError):
            self.store.move_typed('other', 'dest.md', item['version'])
        with self.assertRaises(StoreError):
            self.store.rename_entry('other/a.md', '../bad.md', item['version'])

    def test_newlines_and_empty(self):
        for newline in ['\n', '\r', '\r\n']:
            file = self.store.docs / 'lines.txt'
            file.write_bytes(('a' + newline + 'b' + newline).encode())
            read = self.store.read_document('lines.txt')
            self.assertEqual(read['total_lines'], 2)
            self.assertEqual([r['text'] for r in read['lines']], ['a', 'b'])
            self.store.patch_document('lines.txt', 0, 1, 'c\nd', read['version'])
            self.assertTrue(file.read_bytes().startswith(('c' + newline + 'd').encode()))
        self.store.create_document('empty.md', '')
        self.assertEqual(self.store.read_document('empty.md')['total_lines'], 0)
        self.assertFalse(self.store.read_document_chunk('empty.md')['has_more'])

    def test_retained_chunks_and_fts_boundary(self):
        file = self.store.docs / 'retained.md'
        file.write_bytes(b'x' * (BLOCK_BYTES - 2) + b'ab' + b'c' + b'y' * BLOCK_BYTES)
        self.assertEqual(self.store.search_documents('abc')['total'], 1)
        ids = [r[0] for r in self.store.conn.execute('SELECT id FROM chunks ORDER BY offset')]
        read = self.store.read_document_chunk('retained.md')
        changed = self.store.patch_document('retained.md', BLOCK_BYTES - 2, BLOCK_BYTES, 'de', read['version'])
        self.assertEqual(self.store.search_documents('abc')['total'], 0)
        self.assertEqual(self.store.search_documents('dec')['total'], 1)
        self.assertEqual(ids, [r[0] for r in self.store.conn.execute('SELECT id FROM chunks ORDER BY offset')])
        self.store.move_typed('retained.md', 'moved.md', changed['version'])
        self.assertEqual(ids, [r[0] for r in self.store.conn.execute('SELECT id FROM chunks ORDER BY offset')])
        self.store.conn.execute('DELETE FROM chunk_search')
        self.store.conn.commit()
        self.assertTrue(self.store.refresh_index(full=True)['ok'])
        self.assertEqual(self.store.search_documents('dec')['total'], 1)


class MigrationTests(unittest.TestCase):
    def test_migration_and_invalid_file(self):
        for bad in [False, True]:
            with self.subTest(bad=bad), tempfile.TemporaryDirectory(dir=TEMP) as temp:
                root = Path(temp)
                (root / '.data').mkdir()
                (root / 'knowledge').mkdir()
                (root / 'knowledge' / 'a.md').write_bytes(b'\xff' if bad else b'original')
                db = root / '.data' / 'knowledge.db'
                with closing(sqlite3.connect(db)) as conn, conn:
                    conn.execute('CREATE TABLE documents(path TEXT PRIMARY KEY, name TEXT, size INTEGER, mtime_ns INTEGER, version TEXT, content TEXT)')
                    conn.execute("INSERT INTO documents VALUES ('old.md','old',3,0,'v','old')")
                    conn.execute("CREATE VIRTUAL TABLE search_index USING fts5(path UNINDEXED, location, content, tokenize='trigram')")
                    conn.execute("INSERT INTO search_index VALUES ('old.md','old.md','old')")
                store = KnowledgeStore(root)
                try:
                    self.assertEqual(store.conn.execute('PRAGMA user_version').fetchone()[0], 2)
                    self.assertEqual(store.list_documents()['total'], 0 if bad else 1)
                    if not bad:
                        self.assertEqual(store.search_documents('original')['total'], 1)
                        with self.assertRaises(sqlite3.OperationalError):
                            store.conn.execute('CREATE INDEX documents_mtime ON documents(mtime_ns)')
                finally:
                    store.close()

    def test_startup_structural_failure_rolls_back(self):
        with tempfile.TemporaryDirectory(dir=TEMP) as temp:
            root = Path(temp)
            (root / '.data').mkdir()
            (root / 'knowledge').mkdir()
            (root / 'knowledge' / 'a.md').write_bytes(b'original')
            db = root / '.data' / 'knowledge.db'
            with closing(sqlite3.connect(db)) as conn, conn:
                conn.execute('CREATE TABLE documents(path TEXT PRIMARY KEY, name TEXT, size INTEGER, mtime_ns INTEGER, version TEXT, content TEXT)')
                conn.execute("INSERT INTO documents VALUES ('old.md','old',3,0,'v','old')")
                conn.execute("CREATE VIRTUAL TABLE search_index USING fts5(path UNINDEXED, location, content, tokenize='trigram')")
                conn.execute("INSERT INTO search_index VALUES ('old.md','old.md','old')")
            with patch('knowledge_store.os.scandir', side_effect=PermissionError('denied')):
                with self.assertRaisesRegex(StoreError, '启动同步失败'):
                    KnowledgeStore(root)
            with closing(sqlite3.connect(db)) as conn:
                self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 0)
                self.assertEqual(conn.execute('SELECT content FROM documents').fetchone()[0], 'old')
                self.assertEqual(conn.execute("SELECT count(*) FROM search_index WHERE search_index MATCH 'old'").fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
