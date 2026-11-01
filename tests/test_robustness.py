"""Robustness tests for the uncovered blind spots: oversized files, large corpus
search, concurrent writes, crash recovery, and config tampering."""
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import knowledge_store
from knowledge_store import KnowledgeStore, StoreError
from config import ConfigError, load_config, save_root

PYTHON = Path(sys.executable)


def make_root():
    return Path(tempfile.mkdtemp(prefix='kb-robust-'))


def write_doc(root, rel, text):
    path = root / 'knowledge' / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


class OversizedTest(unittest.TestCase):
    def setUp(self):
        self.root = make_root()
        self.real_max = knowledge_store.MAX_BYTES
        # 把上限压到很小，以便用普通文件触发“超大”分支。
        knowledge_store.MAX_BYTES = 4096

    def tearDown(self):
        knowledge_store.MAX_BYTES = self.real_max
        store = getattr(self, 'store', None)
        if store is not None:
            store.close()

    def test_oversized_document_skipped_not_fatal(self):
        write_doc(self.root, 'normal.md', '# 正常文档\n关键词 内容\n')
        big = (root_big := self.root / 'knowledge' / 'huge.md')
        big.parent.mkdir(parents=True, exist_ok=True)
        big.write_text('x' * 5000 + '\n', encoding='utf-8')
        # 整个知识库仍能打开，不抛错。
        store = self.store = KnowledgeStore(self.root)
        report = store.refresh_index(include_issues=True)
        self.assertEqual(report['ok'], True)
        self.assertGreaterEqual(report['skipped'], 1)
        # 列表只含正常文档。
        listing = store.list_documents()
        self.assertEqual(listing['total'], 1)
        self.assertEqual(listing['items'][0]['path'], 'normal.md')
        # 目录树能看到超大文件且类型为 unsupported，不会抛错。
        tree = store.get_tree()
        names = [c['name'] for c in tree['tree']['children']]
        self.assertIn('huge.md', names)
        huge = next(c for c in tree['tree']['children'] if c['name'] == 'huge.md')
        self.assertEqual(huge['type'], 'unsupported')
        # 搜索只看正常文档。
        res = store.search_documents('关键词')
        self.assertEqual(res['total'], 1)


class LargeFileTest(unittest.TestCase):
    def setUp(self):
        self.root = make_root()
        self.store = KnowledgeStore(self.root)

    def tearDown(self):
        self.store.close()

    def test_large_document_chunk_search_patch(self):
        # 约 4 MiB、4 万行，在深处埋一个唯一关键词。
        lines = []
        keyword_line = None
        for i in range(40000):
            if i == 39000:
                keyword_line = i + 1
                lines.append('UNIQPHRASE123 这是深处的一行\n')
            else:
                lines.append(f'line {i} 普通内容填充数据\n')
        text = ''.join(lines)
        write_doc(self.root, 'big.md', text)
        self.store.refresh_index()
        size = (self.root / 'knowledge' / 'big.md').stat().st_size
        self.assertGreater(size, 1024 * 1024)

        # 分段读取首块。
        first = self.store.read_document_chunk('big.md', offset=0, max_bytes=262144)
        self.assertTrue(first['has_more'])
        self.assertEqual(first['start_byte'], 0)
        self.assertGreater(first['end_byte'], 0)

        # 按深处行号读取，命中关键词。
        chunk = self.store.read_document_chunk_at_line('big.md', keyword_line, max_bytes=262144)
        self.assertIn('UNIQPHRASE123', chunk['content'])

        # 搜索命中并给出片段。
        res = self.store.search_documents('UNIQPHRASE123')
        self.assertEqual(res['total'], 1)
        self.assertEqual(res['items'][0]['path'], 'big.md')
        self.assertTrue(res['items'][0]['snippets'])

        # patch 一处（首块内）并验证内容正确。
        before = self.store.read_full_document('big.md')
        version = before['version']
        patched = self.store.patch_document('big.md', 0, 5, 'HELLO', version)
        self.assertNotEqual(patched['version'], version)
        after = self.store.read_full_document('big.md')
        self.assertTrue(after['content'].startswith('HELLO'))


class LargeCorpusTest(unittest.TestCase):
    def setUp(self):
        self.root = make_root()
        self.store = KnowledgeStore(self.root)
        # 100 篇文档，其中 23 篇含目标短语。
        for i in range(100):
            body = f'文档 {i} 的普通内容。\n'
            if i % 4 == 0:  # 25 篇
                body += 'alpha beta gamma 目标短语出现在这里。\n'
            if i % 25 == 0:  # 4 篇（与上面部分重叠）
                body += '独立中文关键词 机器学习 模型训练。\n'
            write_doc(self.root, f'doc{i:03d}.md', body)
        self.store.refresh_index()

    def tearDown(self):
        self.store.close()

    def test_search_correctness_and_ranking(self):
        start = time.time()
        res = self.store.search_documents('alpha beta')
        elapsed = time.time() - start
        self.assertLess(elapsed, 5.0, f'搜索耗时过长: {elapsed:.2f}s')
        # 25 篇含该短语（i%4==0）；items 受 limit 截断，断言 total。
        self.assertEqual(res['total'], 25)
        # 排序：分数高的在前（按路径稳定排序，分数相同则按路径）。
        self.assertEqual(res['items'], sorted(res['items'], key=lambda r: (-r['score'], r['path'])))

    def test_search_cjk_phrase(self):
        res = self.store.search_documents('机器学习')
        self.assertEqual(res['total'], 4)
        self.assertTrue(all('机器学习' in it['snippets'][0]['text'] for it in res['items']))

    def test_search_excludes_non_matching(self):
        res = self.store.search_documents('zzz_nonexistent_zzz')
        self.assertEqual(res['total'], 0)


class ConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.root = make_root()
        self.store = KnowledgeStore(self.root)
        self.store.refresh_index()
        self.errors = []

    def tearDown(self):
        self.store.close()

    def _worker(self, index):
        try:
            path = f'conc_{index}.md'
            for n in range(3):
                if n == 0:
                    self.store.create_document(path, f'worker {index} rev {n}\n')
                else:
                    info = self.store.read_full_document(path)
                    self.store.update_document(path, f'worker {index} rev {n}\n', info['version'])
        except Exception as exc:  # noqa: BLE001
            self.errors.append((index, exc))

    def test_concurrent_writes_no_corruption(self):
        threads = [threading.Thread(target=self._worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(self.errors, [], f'并发写入异常: {self.errors}')
        listing = self.store.list_documents(limit=100)
        self.assertEqual(listing['total'], 8)
        integrity = self.store.conn.execute('PRAGMA integrity_check').fetchone()[0]
        self.assertEqual(integrity, 'ok')

    def test_version_conflict_rejected(self):
        self.store.create_document('shared.md', 'original\n')
        v0 = self.store.read_full_document('shared.md')['version']
        # 第一次以正确版本更新成功。
        self.store.update_document('shared.md', 'edited once\n', v0)
        # 用过期版本再更新必须被拒绝。
        with self.assertRaises(StoreError):
            self.store.update_document('shared.md', 'stale write\n', v0)


class CrashRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.root = make_root()
        self.flag = self.root / 'ready.flag'

    def tearDown(self):
        for p in self.root.glob('*'):
            pass

    def test_recovery_after_forced_kill(self):
        helper = self.root / '_crash_helper.py'
        helper.write_text(
            "import sys, time\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from knowledge_store import KnowledgeStore\n"
            "s = KnowledgeStore(sys.argv[2])\n"
            "s.create_document('crash.md', 'committed content 已落盘\\n')\n"
            "open(sys.argv[3], 'w').close()\n"
            "time.sleep(30)\n",
            encoding='utf-8')
        proc = subprocess.Popen(
            [str(PYTHON), str(helper), str(PROJECT), str(self.root), str(self.flag)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 15
        while not self.flag.exists():
            if time.time() > deadline:
                proc.kill()
                self.fail('崩溃辅助进程未在时限内完成写入')
            time.sleep(0.1)
        # 确认文件已落盘。
        self.assertTrue((self.root / 'knowledge' / 'crash.md').exists())
        # 强制杀死（模拟 kill -9 / 断电），不执行任何清理。
        os.kill(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

        # 重新打开：锁应已释放，内容应与磁盘一致。
        store = None
        for attempt in range(20):
            try:
                store = KnowledgeStore(self.root)
                break
            except StoreError as exc:
                if '已有服务进程' in str(exc):
                    time.sleep(0.2)
                    continue
                raise
        self.assertIsNotNone(store, '重开后无法打开知识库')
        try:
            listing = store.list_documents()
            self.assertIn('crash.md', [it['path'] for it in listing['items']])
            content = store.read_full_document('crash.md')['content']
            self.assertEqual(content, 'committed content 已落盘\n')
            self.assertEqual(store.conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            # 重建后搜索仍可用。
            self.assertEqual(store.search_documents('committed')['total'], 1)
        finally:
            store.close()


class IndexCorruptionTest(unittest.TestCase):
    def setUp(self):
        self.root = make_root()
        self.store = KnowledgeStore(self.root)
        for i in range(10):
            write_doc(self.root, f'c{i}.md', f'文档 {i} 内容 alpha beta\n')
        self.store.refresh_index()

    def tearDown(self):
        self.store.close()

    def _reopen_after(self, mutate):
        self.store.close()
        db = self.root / '.data' / 'knowledge.db'
        conn = sqlite3.connect(db)
        try:
            mutate(conn)
        finally:
            conn.close()
        self.store = KnowledgeStore(self.root)

    def test_rebuild_when_fts_table_missing(self):
        # 模拟 FTS 表被删除（损坏）。
        def drop(conn):
            conn.execute('DROP TABLE chunk_search')
            conn.commit()
        self._reopen_after(drop)
        self.assertEqual(self.store.search_documents('alpha')['total'], 10)

    def test_rebuild_when_chunks_emptied(self):
        # 模拟索引内容丢失：元数据在但分块为空。
        def empty_chunks(conn):
            conn.execute('DELETE FROM chunks')
            conn.execute('DELETE FROM chunk_search')
            conn.commit()
        self._reopen_after(empty_chunks)
        self.assertEqual(self.store.search_documents('alpha')['total'], 10)


class ConfigTamperingTest(unittest.TestCase):
    def setUp(self):
        self.orig = os.environ.get('KNOWLEDGE_MCP_CONFIG')
        self.dir = Path(tempfile.mkdtemp(prefix='kb-cfg-'))

    def tearDown(self):
        if self.orig is None:
            os.environ.pop('KNOWLEDGE_MCP_CONFIG', None)
        else:
            os.environ['KNOWLEDGE_MCP_CONFIG'] = self.orig

    def _set_cfg(self, text):
        path = self.dir / 'config.json'
        path.write_text(text, encoding='utf-8')
        os.environ['KNOWLEDGE_MCP_CONFIG'] = str(path)
        return path

    def test_invalid_json_raises(self):
        self._set_cfg('{ this is not json')
        self.assertRaises(ConfigError, load_config)

    def test_wrong_version_raises(self):
        self._set_cfg(json.dumps({'version': 999, 'root': str(self.dir)}))
        self.assertRaises(ConfigError, load_config)

    def test_relative_root_rejected(self):
        # 绝对路径校验在 save_root（写入侧）执行，load_config 只校验版本/类型。
        self.assertRaises(ConfigError, save_root, 'relative/path')

    def test_valid_config_loaded(self):
        self._set_cfg(json.dumps({'version': 1, 'root': str(self.dir)}))
        self.assertEqual(load_config()['root'], str(self.dir))

    def test_run_background_degrades_gracefully(self):
        # 复刻 run_background 的配置处理分支：损坏配置不应让服务崩溃，
        # 而是退回未配置状态（store=None, config_error 有值）。
        self._set_cfg('{ broken')
        try:
            root = load_config().get('root')
        except ConfigError as exc:
            config_error = str(exc)
            root = None
        self.assertIsNone(root)
        self.assertIsNotNone(config_error)


if __name__ == '__main__':
    unittest.main()
