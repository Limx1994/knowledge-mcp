import io
from contextlib import redirect_stderr
import logging
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import crashlog

TEMP = Path(__file__).resolve().parents[1] / '.test-work'
TEMP.mkdir(exist_ok=True)
logging.getLogger('knowledge.crash').addHandler(logging.NullHandler())


class CrashLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEMP)
        self.root = Path(self.temp.name)
        (self.root / 'knowledge').mkdir()
        self.folder = crashlog.crash_folder(self.root)
        self.saved = (sys.excepthook, threading.excepthook, sys.unraisablehook)

    def tearDown(self):
        crashlog.close_fatal()
        crashlog.install('startup', None)
        sys.excepthook, threading.excepthook, sys.unraisablehook = self.saved
        self.temp.cleanup()

    def crash(self, message='boom', role='bridge'):
        try:
            raise ValueError(message)
        except ValueError as exc:
            return crashlog.record_exception(role, exc, root=self.root)

    def test_record_contains_context(self):
        path = self.crash()
        self.assertTrue(path.is_file())
        self.assertEqual(path.parent, self.folder)
        text = path.read_text(encoding='utf-8')
        self.assertIn('进程角色: bridge（MCP 桥接）', text)
        self.assertIn(f'知识库: {self.root}', text)
        self.assertIn('异常: ValueError: boom', text)
        self.assertIn('Traceback (most recent call last)', text)
        self.assertIn(f'PID {os.getpid()}', text)

    def test_records_are_newest_first(self):
        first = self.crash('first')
        self.crash('second')
        os.utime(first, (1_000_000, 1_000_000))
        items = crashlog.records(5, self.root)
        self.assertEqual([item['error'] for item in items], ['ValueError: second', 'ValueError: first'])
        self.assertEqual(items[0]['role'], 'bridge')
        self.assertEqual(items[0]['role_label'], 'MCP 桥接')
        self.assertIn('ValueError: second', items[0]['text'])

    def test_prune_keeps_latest(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        names = []
        for index in range(crashlog.KEEP + 5):
            path = self.folder / f'crash-20200101-000000-1-bridge-{index}.txt'
            path.write_text('旧记录\n', encoding='utf-8')
            os.utime(path, (index, index))
            names.append(path.name)
        self.crash('fresh')
        remaining = {path.name for path in self.folder.glob('*.txt')}
        self.assertEqual(len(remaining), crashlog.KEEP)
        self.assertEqual(remaining & set(names[:5]), set())

    def test_count_and_clear(self):
        self.crash()
        self.crash('again')
        self.assertEqual(crashlog.count(self.root), 2)
        self.assertEqual(crashlog.clear(self.root), 2)
        self.assertEqual(crashlog.count(self.root), 0)
        self.assertTrue(self.folder.is_dir())

    def test_uncaught_hook_records_and_chains(self):
        crashlog.install('background', self.root)
        stream = io.StringIO()
        with redirect_stderr(stream):
            sys.excepthook(ValueError, ValueError('uncaught'), None)
        items = crashlog.records(5, self.root)
        self.assertEqual(len(items), 1)
        self.assertIn('ValueError: uncaught', items[0]['error'])
        self.assertEqual(items[0]['role'], 'background')
        self.assertIn('ValueError: uncaught', stream.getvalue())

    def test_thread_hook_records_thread_name(self):
        crashlog.install('background', self.root)
        with redirect_stderr(io.StringIO()):
            worker = threading.Thread(target=lambda: 1 / 0, name='crash-worker')
            worker.start()
            worker.join(5)
        items = crashlog.records(5, self.root)
        self.assertEqual(len(items), 1)
        self.assertIn('ZeroDivisionError', items[0]['error'])
        self.assertIn('线程: crash-worker', items[0]['text'])
        self.assertIn('线程中未捕获的异常', items[0]['text'])

    def test_unraisable_hook_records_note(self):
        crashlog.install('background', self.root)
        item = type('Unraisable', (), dict(
            exc_type=ValueError, exc_value=ValueError('finalizer'), exc_traceback=None,
            err_msg='Exception ignored in: <object>', object=None))()
        with redirect_stderr(io.StringIO()):
            sys.unraisablehook(item)
        items = crashlog.records(5, self.root)
        self.assertEqual(len(items), 1)
        self.assertIn('ValueError: finalizer', items[0]['error'])
        self.assertIn('Exception ignored in', items[0]['text'])

    def test_fatal_file_removed_when_empty(self):
        path = crashlog.enable_fatal(self.root)
        self.assertTrue(path.is_file())
        self.assertEqual(path.parent, self.folder)
        self.assertEqual(path.name, f'fatal-{os.getpid()}.log')
        crashlog.close_fatal()
        self.assertFalse(path.exists())

    def test_crash_folder_follows_config_when_unconfigured(self):
        config = self.root / 'profile' / 'config.json'
        with patch.dict(os.environ, {'KNOWLEDGE_MCP_CONFIG': str(config)}):
            self.assertEqual(crashlog.crash_folder(None), config.parent / 'crashes')
            self.assertEqual(crashlog.crash_folder(self.root), self.folder)

    def test_record_includes_log_tail(self):
        data = self.root / '.data'
        data.mkdir(exist_ok=True)
        (data / 'service.log').write_text('第一行\n最后一行\n', encoding='utf-8')
        text = self.crash().read_text(encoding='utf-8')
        self.assertIn('最近日志（service.log 末尾）:', text)
        self.assertIn('最后一行', text)

    def test_record_includes_other_thread_stack(self):
        release = threading.Event()
        worker = threading.Thread(target=release.wait, name='idle-worker', daemon=True)
        worker.start()
        try:
            text = self.crash().read_text(encoding='utf-8')
        finally:
            release.set()
            worker.join(5)
        self.assertIn('其他线程栈:', text)
        self.assertIn('idle-worker', text)

    def test_long_message_is_truncated_in_header(self):
        text = self.crash('长' * 5000).read_text(encoding='utf-8')
        header = next(line for line in text.splitlines() if line.startswith('异常: '))
        self.assertLess(len(header), 2100)

    def test_records_flag_truncated_preview(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / 'crash-20200101-000000-1-bridge.txt'
        path.write_text('异常: ValueError: big\n' + 'x' * (crashlog.MAX_PREVIEW + 100), encoding='utf-8')
        items = crashlog.records(5, self.root)
        self.assertTrue(items[0]['truncated'])
        self.assertEqual(len(items[0]['text']), crashlog.MAX_PREVIEW)

    def test_write_failure_returns_none(self):
        with patch.object(Path, 'write_text', side_effect=OSError('denied')):
            with self.assertLogs('knowledge.crash', level='ERROR'):
                self.assertIsNone(self.crash())


if __name__ == '__main__':
    unittest.main()
