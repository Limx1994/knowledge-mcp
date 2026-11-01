import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from config import ConfigError, config_path, load_config, save_root

PROJECT = Path(__file__).resolve().parents[1]
TEMP = PROJECT / '.test-work'
TEMP.mkdir(exist_ok=True)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEMP)
        self.folder = Path(self.temp.name)
        self.path = self.folder / 'config.json'
        self.env = patch.dict(os.environ, {'KNOWLEDGE_MCP_CONFIG': str(self.path)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_missing_config(self):
        self.assertEqual(config_path(), self.path)
        self.assertEqual(load_config(), {})

    def test_roundtrip(self):
        root = self.folder / '中文 知识库'
        root.mkdir()
        saved = save_root(root)
        self.assertEqual(saved, self.path)
        data = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(data, {'version': 1, 'root': str(root)})
        self.assertEqual(load_config()['root'], str(root))
        self.assertEqual(list(self.folder.glob('.config-*')), [])

    def test_corrupt_and_version(self):
        self.path.write_text('{oops', encoding='utf-8')
        with self.assertRaises(ConfigError):
            load_config()
        self.path.write_text('{"version": 99, "root": "C:\\\\x"}', encoding='utf-8')
        with self.assertRaises(ConfigError):
            load_config()

    def test_relative_root_rejected(self):
        with self.assertRaises(ConfigError):
            save_root('relative/path')


if __name__ == '__main__':
    unittest.main()
