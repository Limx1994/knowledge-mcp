"""Per-user configuration; the knowledge base path survives EXE moves and restarts."""
import json
import os
import tempfile
from pathlib import Path

VERSION = 1


class ConfigError(Exception):
    pass


def config_path():
    override = os.environ.get('KNOWLEDGE_MCP_CONFIG')
    if override:
        return Path(override).expanduser()
    base = os.environ.get('LOCALAPPDATA') or os.environ.get('APPDATA')
    if base:
        return Path(base) / 'LocalKnowledgeMcp' / 'config.json'
    return Path.home() / '.local-knowledge-mcp' / 'config.json'


def load_config():
    path = config_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ConfigError(f'配置文件无法读取: {path}: {exc}') from exc
    if not isinstance(data, dict) or data.get('version') != VERSION:
        raise ConfigError(f'配置文件版本不支持: {path}')
    root = data.get('root')
    if root is not None and (not isinstance(root, str) or not root):
        raise ConfigError(f'配置文件中的知识库路径无效: {path}')
    return data


def save_root(root):
    root = Path(root)
    if not root.is_absolute():
        raise ConfigError('知识库路径必须是绝对路径')
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({'version': VERSION, 'root': str(root)}, ensure_ascii=False, indent=2) + '\n'
    temp = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent,
                                         prefix='.config-', delete=False) as stream:
            temp = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp is not None and temp.exists():
            temp.unlink()
    return path
