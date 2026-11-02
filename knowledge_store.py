"""File-backed knowledge store and rebuildable SQLite search index."""
import codecs
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import sqlite3
import stat
import tempfile
import threading
import uuid
from datetime import datetime, timezone

from document_stream import (MAX_BYTES, FULL_BYTES, BLOCK_BYTES, BREAKS,
                             inspect_file, read_block, copy_bytes, search_chunks)


# 读取前同步策略（仅影响性能，不改变写入路径）。
# KNOWLEDGE_MCP_SYNC_INTERVAL:
#   "always"/"0"/未设置 -> 每次读取前都全目录同步（默认，行为不变）
#   "lazy"              -> 仅首次同步，之后复用索引；外部编辑需手动 refresh_index
#   正数秒(如 "2")       -> 防抖：距上次成功同步不足 N 秒则跳过全目录 walk
def _parse_sync_interval():
    raw = os.environ.get('KNOWLEDGE_MCP_SYNC_INTERVAL', '0').strip().lower()
    if raw in ('', '0', 'always', 'off'):
        return 0
    if raw == 'lazy':
        return None
    try:
        return float(raw) if float(raw) > 0 else 0
    except ValueError:
        return 0


_SYNC_INTERVAL = _parse_sync_interval()


def _sync_elapsed(completed_at):
    try:
        then = datetime.fromisoformat(completed_at)
    except (ValueError, TypeError):
        return float('inf')
    return (datetime.now(timezone.utc) - then).total_seconds()


class StoreError(Exception):
    pass


class TargetExists(StoreError, FileExistsError):
    """Target already exists; remains compatible with FileExistsError callers."""


def version(data):
    return hashlib.sha256(data).hexdigest()


def path_key(value):
    """Path identity key matching the Windows filesystem, not Unicode casefold."""
    return os.path.normcase(value)


def check_link(path):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise StoreError(f'拒绝重解析点: {path}')


def is_reparse(entry):
    """True for symlinks and Windows reparse points, without following them."""
    if entry.is_symlink():
        return True
    info = entry.stat(follow_symlinks=False)
    return bool(getattr(info, 'st_file_attributes', 0) & 0x400)


class KnowledgeStore:
    def __init__(self, root):
        root = Path(root)
        if not root.is_absolute() or not root.is_dir():
            raise StoreError('--root 必须是已存在的绝对目录')
        # Resolve junctions/symlinks in the root path itself; reparse points
        # inside knowledge/ remain rejected or skipped by the scan.
        self.root = root.resolve()
        for part in [self.root, *self.root.parents]:
            check_link(part)
        self.guard = threading.RLock()
        self.conn = None
        self.lock_file = None
        self.last_sync = None
        self.docs = self.root / 'knowledge'
        self.data = self.root / '.data'
        self.trash = self.root / '.trash'
        for folder in [self.docs, self.data, self.trash]:
            if folder.exists() or folder.is_symlink():
                check_link(folder)
            folder.mkdir(exist_ok=True)
        try:
            lock_path = self.data / 'service.lock'
            if lock_path.exists():
                check_link(lock_path)
            self.lock_file = open(lock_path, 'a+b')
            if os.fstat(self.lock_file.fileno()).st_size == 0:
                self.lock_file.write(b'0')
                self.lock_file.flush()
            self.lock_file.seek(0)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(self.lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise StoreError('该知识库已有服务进程运行') from exc
            db = self.data / 'knowledge.db'
            for file in [db, Path(str(db) + '-journal'), Path(str(db) + '-wal'), Path(str(db) + '-shm')]:
                if file.exists() or file.is_symlink():
                    check_link(file)
            self.conn = sqlite3.connect(db, timeout=3, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute('PRAGMA cache_size=-32768')
            self.conn.execute('PRAGMA temp_store=FILE')
            self._initialize_index()
        except Exception:
            self.close()
            raise

    def close(self):
        if self.conn is not None:
            self.conn.close()
            self.conn = None
        if self.lock_file is not None:
            self.lock_file.close()
            self.lock_file = None

    @staticmethod
    def _parts(value, directory=False):
        if not isinstance(value, str):
            raise StoreError('路径必须是字符串')
        value = value.replace('\\', '/')
        if not value and directory:
            return []
        win = PureWindowsPath(value)
        parts = value.split('/')
        if win.drive or win.root or any(p in ('', '.', '..') for p in parts):
            raise StoreError('只允许知识目录内的相对路径')
        for part in parts:
            if re.search(r'[<>:"|?*\x00-\x1f]', part) or part.endswith((' ', '.')):
                raise StoreError('非法 Windows 路径')
            if re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])', part.split('.')[0], re.I):
                raise StoreError('拒绝 Windows 保留名称')
        return parts

    def _path(self, value, directory=False):
        parts = self._parts(value, directory)
        if not parts and directory:
            return self.docs
        result = self.docs
        check_link(result)
        for part in parts:
            result = result / part
            if result.exists() or result.is_symlink():
                check_link(result)
        if not directory and result.suffix.lower() not in ('.md', '.txt'):
            raise StoreError('仅支持 .md 和 .txt')
        return result

    def _load(self, path):
        check_link(path)
        with path.open('rb') as stream:
            data = stream.read(FULL_BYTES + 1)
        if len(data) > FULL_BYTES:
            raise StoreError('全文操作超过 10 MiB，请使用分段读取和 patch_document')
        return data, data.decode('utf-8-sig')

    def _initialize_index(self):
        schema = self.conn.execute('PRAGMA user_version').fetchone()[0]
        if schema not in (0, 2):
            raise StoreError(f'不支持的索引版本: {schema}')
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            # schema==2 但核心表被损坏或丢失，或索引内容被清空（元数据有但分块为空），
            # 都要重建，否则 refresh_index 会因 no such table 或空索引导致整库不可用。
            existing = {row['name']: row['type'] for row in self.conn.execute(
                "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view')")}
            rebuild = schema != 2 or not ({'document_meta', 'chunks', 'chunk_search', 'documents'} <= set(existing))
            full = rebuild
            if not rebuild:
                counts = self.conn.execute(
                    'SELECT (SELECT count(*) FROM document_meta), (SELECT count(*) FROM chunks)').fetchone()
                if counts[0] and not counts[1]:
                    rebuild = True
                    full = True
            if rebuild:
                self.conn.execute('DROP TABLE IF EXISTS search_index')
                self.conn.execute('DROP TABLE IF EXISTS document_meta')
                self.conn.execute('DROP TABLE IF EXISTS chunks')
                self.conn.execute('DROP TABLE IF EXISTS chunk_search')
                # documents 在旧 schema 中是表，在 v2 中是视图，按类型删除。
                if existing.get('documents') == 'view':
                    self.conn.execute('DROP VIEW IF EXISTS documents')
                elif existing.get('documents') == 'table':
                    self.conn.execute('DROP TABLE IF EXISTS documents')
                self.conn.execute("""CREATE TABLE document_meta (
                    path TEXT PRIMARY KEY COLLATE NOCASE, name TEXT NOT NULL,
                    size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, version TEXT NOT NULL,
                    total_lines INTEGER NOT NULL, bom INTEGER NOT NULL, newline TEXT NOT NULL)""")
                self.conn.execute('CREATE INDEX meta_mtime ON document_meta(mtime_ns)')
                self.conn.execute("""CREATE TABLE chunks (
                    id INTEGER PRIMARY KEY, path TEXT COLLATE NOCASE NOT NULL,
                    offset INTEGER NOT NULL, size INTEGER NOT NULL,
                    start_line INTEGER NOT NULL, content TEXT NOT NULL)""")
                self.conn.execute('CREATE INDEX chunks_path ON chunks(path,offset)')
                self.conn.execute("""CREATE VIRTUAL TABLE chunk_search USING fts5(
                    content, tokenize='trigram', content='', contentless_delete=1, detail=none)""")
                # Old executables fail at CREATE INDEX on this read-only view.
                self.conn.execute('CREATE VIEW documents AS SELECT path,name,size,mtime_ns,version FROM document_meta')
                self.conn.execute('PRAGMA user_version=2')
            # Added after v2 shipped; old executables simply ignore the extra index.
            self.conn.execute('CREATE INDEX IF NOT EXISTS chunks_line ON chunks(path,start_line)')
            report = self.refresh_index(full=full, _nested=True)
            if not report['ok']:
                raise StoreError('启动同步失败: ' + json.dumps(report, ensure_ascii=False))

    def _remove_index(self, path):
        self.conn.execute('DELETE FROM chunk_search WHERE rowid IN (SELECT id FROM chunks WHERE path=?)', (path,))
        self.conn.execute('DELETE FROM chunks WHERE path=?', (path,))
        self.conn.execute('DELETE FROM document_meta WHERE path=?', (path,))

    def _index_file(self, file, rel, full=False):
        tail = ''
        old_tail = ''
        retained = set()

        def consume(offset, data, text, line):
            nonlocal tail, old_tail
            prior = self.conn.execute('SELECT id,content FROM chunks WHERE path=? AND offset=?', (rel, offset)).fetchone()
            if prior:
                row_id = prior['id']
                if prior['content'] != text:
                    self.conn.execute('UPDATE chunks SET path=?,size=?,start_line=?,content=? WHERE id=?',
                                      (rel, len(data), line, text, row_id))
                else:
                    self.conn.execute('UPDATE chunks SET path=?,start_line=? WHERE id=? AND (path<>? COLLATE BINARY OR start_line<>?)',
                                      (rel, line, row_id, rel, line))
                if full or prior['content'] != text or tail != old_tail:
                    self.conn.execute('DELETE FROM chunk_search WHERE rowid=?', (row_id,))
                    self.conn.execute('INSERT INTO chunk_search(rowid,content) VALUES (?,?)', (row_id, tail + text))
            else:
                cursor = self.conn.execute('INSERT INTO chunks(path,offset,size,start_line,content) VALUES (?,?,?,?,?)',
                                           (rel, offset, len(data), line, text))
                row_id = cursor.lastrowid
                self.conn.execute('INSERT INTO chunk_search(rowid,content) VALUES (?,?)', (row_id, tail + text))
            retained.add(row_id)
            old_tail = prior['content'][-2:] if prior else ''
            tail = text[-2:]

        info = inspect_file(file, consume)
        rows = self.conn.execute('SELECT id FROM chunks WHERE path=?', (rel,)).fetchall()
        for row in rows:
            if row['id'] not in retained:
                self.conn.execute('DELETE FROM chunk_search WHERE rowid=?', (row['id'],))
                self.conn.execute('DELETE FROM chunks WHERE id=?', (row['id'],))
        self.conn.execute('INSERT OR REPLACE INTO document_meta VALUES (?,?,?,?,?,?,?,?)',
                          (rel, file.name, info['size'], info['mtime_ns'], info['version'],
                           info['total_lines'], info['bom'], info['newline']))
        return info

    def refresh_index(self, full=False, _nested=False, include_issues=True):
        with self.guard:
            report = dict(ok=True, added=0, updated=0, deleted=0,
                          unchanged=0, skipped=0, failed=0, issues=[],
                          issues_total=0, issues_truncated=False)
            old = {path_key(r['path']): dict(r) for r in self.conn.execute(
                'SELECT path,size,mtime_ns FROM document_meta')}
            found = set()

            def issue(path, exc, skipped=False):
                key = 'skipped' if skipped else 'failed'
                report[key] += 1
                report['issues_total'] += 1
                if include_issues and len(report['issues']) < 200:
                    report['issues'].append(dict(path=str(path), reason=str(exc), kind=key))
                elif include_issues:
                    report['issues_truncated'] = True

            def walk(folder):
                try:
                    check_link(folder)
                    with os.scandir(folder) as entries:
                        items = sorted(entries, key=lambda e: e.name)
                except (OSError, StoreError) as exc:
                    issue(folder, exc)
                    return
                for entry in items:
                    file = Path(entry.path)
                    rel = file.relative_to(self.docs).as_posix()
                    try:
                        # A single junction or symlink must not block the whole library.
                        if is_reparse(entry):
                            issue(rel, '跳过重解析点', True)
                            continue
                        if entry.is_dir():
                            walk(file)
                        elif os.path.splitext(entry.name)[1].lower() not in ('.md', '.txt'):
                            issue(rel, '不支持的扩展名', True)
                        else:
                            # Syntax only; links were already rejected above.
                            self._parts(rel)
                            key = path_key(rel)
                            if key in found:
                                raise StoreError('路径大小写冲突')
                            found.add(key)
                            # 超大文档只跳过索引，不能让整个知识库打不开。
                            if entry.stat().st_size > MAX_BYTES:
                                issue(rel, '文档超过 1.5 GiB，已跳过索引', True)
                                continue
                            info = entry.stat()
                            prior = old.get(key)
                            if not full and prior and prior['path'] == rel and prior['size'] == info.st_size and prior['mtime_ns'] == info.st_mtime_ns:
                                report['unchanged'] += 1
                                continue
                            self._index_file(file, rel, full)
                            report['updated' if prior else 'added'] += 1
                    except sqlite3.Error as exc:
                        issue(rel, exc)
                    except (OSError, UnicodeError, StoreError, ValueError) as exc:
                        # A single unreadable, locked, or changing file must not
                        # block the rest of the library.
                        issue(rel, exc, True)

            try:
                self.conn.execute('SAVEPOINT refresh')
                walk(self.docs)
                if not report['failed']:
                    for key, row in old.items():
                        if key not in found:
                            self._remove_index(row['path'])
                            report['deleted'] += 1
                if report['failed']:
                    self.conn.execute('ROLLBACK TO refresh')
                self.conn.execute('RELEASE refresh')
            except sqlite3.Error as exc:
                if self.conn.in_transaction and not _nested:
                    self.conn.rollback()
                issue('knowledge.db', exc)
            finally:
                del walk
            if report['failed']:
                report.update(ok=False, added=0, updated=0, deleted=0)
            self.last_sync = dict(report, issues=[], completed_at=datetime.now(timezone.utc).isoformat())
            return report

    def _sync(self):
        # 条件同步：开启 KNOWLEDGE_MCP_SYNC_INTERVAL 且上次同步成功后，
        # 跳过全目录 walk，复用最近一次同步统计。库内写入已即时更新索引，
        # 跳过只延迟对“外部编辑器改动”的发现（最多一个防抖窗口）。
        #   _SYNC_INTERVAL == 0 -> 每次都同步（默认，行为不变）
        #   _SYNC_INTERVAL is None (lazy) -> 跳过，仅首次同步
        #   _SYNC_INTERVAL > 0 (防抖) -> 距上次成功同步不足 N 秒则跳过
        last = self.last_sync
        if last is not None and last.get('ok'):
            if _SYNC_INTERVAL is None:
                return last
            if _SYNC_INTERVAL > 0 and _sync_elapsed(last.get('completed_at')) < _SYNC_INTERVAL:
                return last
        report = self.refresh_index(include_issues=False)
        if not report['ok']:
            raise StoreError('索引同步失败: ' + json.dumps(report, ensure_ascii=False))
        return report

    @staticmethod
    def _page(offset, limit):
        if offset < 0 or not 1 <= limit <= 100:
            raise StoreError('offset 必须 >=0，limit 必须在 1..100')

    def _directory_filter(self, directory, recursive):
        folder = self._path(directory, directory=True)
        prefix = folder.relative_to(self.docs).as_posix()
        prefix = '' if prefix == '.' else prefix + '/'
        pattern = prefix.replace('!', '!!').replace('%', '!%').replace('_', '!_') + '%'
        where = "d.path LIKE ? ESCAPE '!' "
        params = [pattern]
        if not recursive:
            where += "AND instr(substr(d.path, ?), '/')=0 "
            params.append(len(prefix) + 1)
        return where, params

    def list_documents(self, directory='', recursive=True, offset=0, limit=20):
        with self.guard:
            self._page(offset, limit)
            sync = self._sync()
            where, params = self._directory_filter(directory, recursive)
            total = self.conn.execute('SELECT count(*) FROM documents d WHERE ' + where,
                                      params).fetchone()[0]
            rows = self.conn.execute(
                'SELECT path,name,size,mtime_ns,version FROM documents d WHERE ' + where +
                'ORDER BY d.path LIMIT ? OFFSET ?', [*params, limit, offset]).fetchall()
            return dict(total=total, items=[dict(r) for r in rows], sync=sync)

    @staticmethod
    def _words(query):
        """Split on whitespace; a balanced double-quoted span stays one phrase."""
        tokens = []
        index, length = 0, len(query)
        while index < length:
            while index < length and query[index].isspace():
                index += 1
            if index >= length:
                break
            if query[index] == '"':
                end = query.find('"', index + 1)
                if end != -1:
                    phrase = query[index + 1:end].strip()
                    if phrase:
                        tokens.append(phrase)
                    index = end + 1
                    continue
            start = index
            while index < length and not query[index].isspace():
                index += 1
            tokens.append(query[start:index])
        return tokens

    @staticmethod
    def _indexable(word):
        return '\x00' not in word and all(
            char.isascii() or '\u4e00' <= char <= '\u9fff' for char in word)

    def _short_candidates(self, word, where, params):
        """Candidate paths for a 1-2 character word without trigrams."""
        cjk = all('\u4e00' <= char <= '\u9fff' for char in word)
        text = 'd.content' if cjk else 'lower(d.content)'
        if len(word) == 1:
            sql = ('SELECT DISTINCT d.path FROM chunks d '
                   f'WHERE instr({text}, ?) > 0 AND ' + where)
            args = [word, *params]
        else:
            edge = ('substr(d.content,-1)' if cjk else 'lower(substr(d.content,-1))')
            edge += ' || ' + ('substr(n.content,1,1)' if cjk else 'lower(substr(n.content,1,1))')
            sql = ('SELECT DISTINCT d.path FROM chunks d '
                   f'WHERE instr({text}, ?) > 0 AND ' + where +
                   ' UNION SELECT DISTINCT d.path FROM chunks d '
                   'JOIN chunks n ON n.path=d.path AND n.offset=d.offset+d.size '
                   f'WHERE instr({edge}, ?) > 0 AND ' + where)
            args = [word, *params, word, *params]
        rows = self.conn.execute(sql, args)
        try:
            return {row['path'] for row in rows}
        finally:
            rows.close()

    def search_documents(self, query, directory='', recursive=True, offset=0, limit=20):
        with self.guard:
            self._page(offset, limit)
            words = list(dict.fromkeys(word.lower() for word in self._words(query)))
            if not words:
                raise StoreError('query 不能为空')
            filters = []
            for word in words:
                # Keep Unicode case expansions on the exact scan path.
                if len(word) >= 3 and self._indexable(word):
                    middle = (len(word) - 3) // 2
                    filters.append(tuple(dict.fromkeys((word[:3], word[middle:middle + 3], word[-3:]))))
                else:
                    filters.append(())
            sync = self._sync()
            where, params = self._directory_filter(directory, recursive)
            results = []
            candidates = {}
            short_candidates = {}
            cursor = self.conn.execute('SELECT * FROM document_meta d WHERE ' + where + 'ORDER BY d.path', params)
            try:
                for row in cursor:
                    location = row['path'].lower()
                    candidate = True
                    for word, grams in zip(words, filters):
                        if word in location:
                            continue
                        if grams:
                            # Separate path sets allow the trigrams to occur in
                            # different chunks, including a word spanning a boundary.
                            for gram in grams:
                                if gram not in candidates:
                                    phrase = '"' + gram.replace('"', '""') + '"'
                                    hits = self.conn.execute('SELECT DISTINCT d.path FROM chunk_search JOIN chunks d ON d.id=chunk_search.rowid WHERE chunk_search MATCH ? AND ' + where,
                                                             [phrase, *params])
                                    try:
                                        candidates[gram] = {hit['path'] for hit in hits}
                                    finally:
                                        hits.close()
                                if row['path'] not in candidates[gram]:
                                    candidate = False
                                    break
                        elif self._indexable(word):
                            # Short words have no trigram; narrow candidates in
                            # SQL instead of scanning every document.
                            if word not in short_candidates:
                                short_candidates[word] = self._short_candidates(word, where, params)
                            if row['path'] not in short_candidates[word]:
                                candidate = False
                        if not candidate:
                            break
                    if not candidate:
                        continue
                    chunks = self.conn.execute('SELECT content FROM chunks WHERE path=? ORDER BY offset', (row['path'],))
                    try:
                        counts, snippets = search_chunks(chunks, words)
                    finally:
                        chunks.close()
                    if all(count or word in location for word, count in zip(words, counts)):
                        results.append(dict(path=row['path'], version=row['version'], score=sum(counts), snippets=snippets))
            finally:
                cursor.close()
            results.sort(key=lambda r: (-r['score'], r['path']))
            return dict(total=len(results), items=results[offset:offset + limit], sync=sync)

    def _metadata(self, path):
        row = self.conn.execute('SELECT * FROM document_meta WHERE path=?', (path,)).fetchone()
        if row is None:
            raise FileNotFoundError(path)
        return dict(row)

    def _line_anchor(self, file, meta, start_line):
        """Locate a chunk that contains the start of start_line, at most one chunk early."""
        rows = self.conn.execute(
            'SELECT offset,start_line FROM chunks WHERE path=? AND start_line<=? '
            'ORDER BY start_line DESC LIMIT 2', (meta['path'], start_line)).fetchall()
        if not rows:
            return 0, 1
        floor = 3 if meta['bom'] else 0
        row = rows[0]
        if row['start_line'] < start_line or row['offset'] <= floor:
            return row['offset'], row['start_line']
        # The chunk starts on the target line; include the previous chunk when
        # that line began before this chunk.
        with file.open('rb') as stream:
            stream.seek(row['offset'] - 1)
            boundary = stream.read(1) in (b'\n', b'\r')
        if boundary:
            return row['offset'], row['start_line']
        if len(rows) > 1 and rows[1]['start_line'] < start_line:
            return rows[1]['offset'], rows[1]['start_line']
        return 0, 1

    def read_document(self, path, start_line=1, max_lines=200):
        with self.guard:
            if start_line < 1 or not 1 <= max_lines <= 1000:
                raise StoreError('start_line 必须 >=1，max_lines 必须在 1..1000')
            sync = self._sync()
            file = self._path(path)
            meta = self._metadata(file.relative_to(self.docs).as_posix())
            lines = []
            pending = ''
            offset, number = self._line_anchor(file, meta, start_line)
            used = 0
            chunks = self.conn.execute('SELECT content FROM chunks WHERE path=? AND offset>=? ORDER BY offset',
                                       (meta['path'], offset))
            try:
                for row in chunks:
                    text = pending + row['content']
                    pos = 0
                    for match in BREAKS.finditer(text):
                        if start_line <= number < start_line + max_lines:
                            value = text[pos:match.start()]
                            used += len(value.encode('utf-8'))
                            if used > BLOCK_BYTES:
                                raise StoreError('读取内容超过 1 MiB，请使用 read_document_chunk')
                            lines.append(dict(line=number, text=value))
                        number += 1
                        pos = match.end()
                        if number >= start_line + max_lines:
                            break
                    if number >= start_line + max_lines:
                        break
                    pending = text[pos:] if number >= start_line else ''
                    if len(pending.encode('utf-8')) > BLOCK_BYTES:
                        raise StoreError('单行超过 1 MiB，请使用 read_document_chunk')
                else:
                    if pending and start_line <= number < start_line + max_lines:
                        used += len(pending.encode('utf-8'))
                        if used > BLOCK_BYTES:
                            raise StoreError('读取内容超过 1 MiB，请使用 read_document_chunk')
                        lines.append(dict(line=number, text=pending))
            finally:
                chunks.close()
            return dict(path=meta['path'], version=meta['version'], total_lines=meta['total_lines'],
                        has_more=start_line - 1 + max_lines < meta['total_lines'], lines=lines, sync=sync)

    def _bytes(self, content, original=b''):
        content = content.replace('\r\n', '\n').replace('\r', '\n')
        if b'\r\n' in original:
            content = content.replace('\n', '\r\n')
        elif b'\r' in original:
            content = content.replace('\n', '\r')
        data = content.encode('utf-8')
        if original.startswith(codecs.BOM_UTF8):
            data = codecs.BOM_UTF8 + data
        if len(data) > FULL_BYTES:
            raise StoreError('全文操作超过 10 MiB，请使用分段读取和 patch_document')
        return data

    def read_full_document(self, path):
        with self.guard:
            sync = self._sync()
            file = self._path(path)
            data, content = self._load(file)
            return dict(path=file.relative_to(self.docs).as_posix(), content=content,
                        version=version(data), sync=sync)

    def read_document_chunk(self, path, offset=0, max_bytes=262144):
        with self.guard:
            if offset < 0 or not 8 <= max_bytes <= BLOCK_BYTES:
                raise StoreError('offset 必须 >=0，max_bytes 必须在 8..1048576')
            sync = self._sync()
            file = self._path(path)
            meta = self._metadata(file.relative_to(self.docs).as_posix())
            if offset > meta['size']:
                raise StoreError('offset 超过文件大小')
            with file.open('rb') as stream:
                start = max(offset, 3 if meta['bom'] else 0)
                stream.seek(start)
                # Arbitrary jumps align backwards to the containing character.
                while start > (3 if meta['bom'] else 0):
                    byte = stream.read(1)
                    if not byte or byte[0] & 0xc0 != 0x80:
                        break
                    start -= 1
                    stream.seek(start)
                if start:
                    stream.seek(start - 1)
                    if stream.read(2) == b'\r\n':
                        start -= 1
                stream.seek(start)
                data = read_block(stream, max_bytes)
                end = start + len(data)
            row = self.conn.execute('SELECT offset,start_line,content FROM chunks WHERE path=? AND offset<=? ORDER BY offset DESC LIMIT 1',
                                    (meta['path'], start)).fetchone()
            line = 1
            if row:
                prefix = row['content'].encode('utf-8')[:start - row['offset']].decode('utf-8')
                line = row['start_line'] + len(BREAKS.findall(prefix))
            return dict(path=meta['path'], content=data.decode('utf-8'), start_byte=start,
                        end_byte=end, next_offset=end, total_bytes=meta['size'], start_line=line,
                        version=meta['version'], bom=bool(meta['bom']), has_more=end < meta['size'], sync=sync)

    def _line_offset(self, file, meta, line):
        offset, number = self._line_anchor(file, meta, line)
        if number >= line:
            return offset
        with file.open('rb') as stream:
            stream.seek(offset)
            remaining = line - number
            while remaining:
                data = read_block(stream, 65536)
                if not data:
                    return offset
                text = data.decode('utf-8')
                for match in BREAKS.finditer(text):
                    remaining -= 1
                    if not remaining:
                        return offset + len(text[:match.end()].encode('utf-8'))
                offset += len(data)
        return offset

    def read_document_chunk_at_line(self, path, line, max_bytes=262144):
        with self.guard:
            if line < 1:
                raise StoreError('line 必须 >=1')
            self._sync()
            file = self._path(path)
            meta = self._metadata(file.relative_to(self.docs).as_posix())
            if line > meta['total_lines']:
                raise StoreError('line 超过文档总行数')
            offset = self._line_offset(file, meta, line)
        return self.read_document_chunk(path, offset, max_bytes)

    @staticmethod
    def _boundary(stream, position, size, bom):
        if position < (3 if bom else 0) or position > size:
            raise StoreError('编辑范围无效或包含 BOM')
        if position < size:
            stream.seek(position)
            if stream.read(1)[0] & 0xc0 == 0x80:
                raise StoreError('编辑范围截断 UTF-8 字符')
        if 0 < position < size:
            stream.seek(position - 1)
            if stream.read(2) == b'\r\n':
                raise StoreError('编辑范围截断 CRLF')

    def patch_document(self, path, start_byte, end_byte, content, expected_version):
        with self.guard:
            if len(content.encode('utf-8')) > BLOCK_BYTES:
                raise StoreError('局部编辑内容超过 1 MiB')
            if start_byte > end_byte:
                raise StoreError('编辑范围无效')
            file = self._path(path)
            rel = file.relative_to(self.docs).as_posix()
            info = self._check_version(file, expected_version)
            original = codecs.BOM_UTF8 if info['bom'] else b''
            original += info['newline'].encode('ascii')
            data = self._bytes(content, original)
            if info['bom']:
                data = data[3:]
            if len(data) > BLOCK_BYTES:
                raise StoreError('换行转换后的局部编辑内容超过 1 MiB')
            size = info['size'] - (end_byte - start_byte) + len(data)
            if size > MAX_BYTES:
                raise StoreError('文档超过 1.5 GiB')
            temp = None
            try:
                with file.open('rb') as source:
                    self._boundary(source, start_byte, info['size'], info['bom'])
                    self._boundary(source, end_byte, info['size'], info['bom'])
                    source.seek(0)
                    with tempfile.NamedTemporaryFile(dir=file.parent, prefix='.update-', delete=False) as stream:
                        temp = Path(stream.name)
                        copy_bytes(source, stream, start_byte)
                        stream.write(data)
                        source.seek(end_byte)
                        copy_bytes(source, stream)
                        stream.flush()
                        os.fsync(stream.fileno())
                self._path(path)
                if not self._unchanged(file, info):
                    raise StoreError('版本冲突，请重新读取')
                os.replace(temp, file)
            finally:
                if temp is not None and temp.exists():
                    temp.unlink()
            try:
                written = self._index_one(file, rel)
            except Exception as exc:
                raise StoreError(f'文件已修改、索引未同步: {rel}: {exc}') from exc
            return dict(ok=True, path=rel, version=written['version'], start_byte=start_byte,
                        end_byte=start_byte + len(data), total_bytes=written['size'])

    def list_entries(self, directory='', offset=0, limit=20):
        with self.guard:
            self._page(offset, limit)
            sync = self._sync()
            tree = self._snapshot(self._path(directory, directory=True))
            items = [{k: v for k, v in item.items() if k != 'children'} for item in tree['children']]
            return dict(path=tree['path'], version=tree['version'], total=len(items),
                        unsupported=tree['unsupported'], items=items[offset:offset + limit], sync=sync)

    def move_typed(self, path, target, expected_version, directory=False):
        with self.guard:
            source = self._path(path, directory=directory)
            if source.is_dir() != directory:
                raise StoreError('文档或目录类型不匹配')
            return self.move_entry(path, target, expected_version)

    def rename_entry(self, path, new_name, expected_version, directory=False):
        if not new_name or '/' in new_name or '\\' in new_name:
            raise StoreError('新名称不能为空或包含路径分隔符')
        source = self._path(path, directory=directory)
        target = source.with_name(new_name).relative_to(self.docs).as_posix()
        return self.move_typed(path, target, expected_version, directory)

    def _snapshot(self, folder, metas=None):
        check_link(folder)
        if metas is None:
            metas = {r['path']: dict(r) for r in self.conn.execute(
                'SELECT path,version,size,mtime_ns FROM document_meta')}
        with os.scandir(folder) as entries:
            items = sorted(entries, key=lambda e: (e.name.casefold(), e.name))
        children = []
        for entry in items:
            file = Path(entry.path)
            rel = file.relative_to(self.docs).as_posix()
            self._parts(rel, directory=True)
            if is_reparse(entry):
                continue
            if entry.is_dir():
                node = self._snapshot(file, metas)
            else:
                info = entry.stat()
                if not stat.S_ISREG(info.st_mode):
                    raise StoreError(f'不支持的文件类型: {entry.name}')
                kind = 'document' if os.path.splitext(entry.name)[1].lower() in ('.md', '.txt') else 'unsupported'
                if kind == 'document':
                    if info.st_size > MAX_BYTES:
                        # 超大文档不索引，也不在快照里当作文档解析，避免 inspect_file 抛错。
                        node = dict(name=entry.name, path=rel, type='unsupported',
                                    version=hashlib.sha256(
                                        f'{info.st_size}:{info.st_mtime_ns}'.encode('ascii')).hexdigest(),
                                    size=info.st_size)
                        children.append(node)
                        continue
                    meta = metas.get(rel)
                    if meta and meta['size'] == info.st_size and meta['mtime_ns'] == info.st_mtime_ns:
                        digest = meta['version']
                    else:
                        digest = inspect_file(file)['version']
                else:
                    digest = hashlib.sha256(f'{info.st_size}:{info.st_mtime_ns}'.encode('ascii')).hexdigest()
                node = dict(name=entry.name, path=rel, type=kind, version=digest, size=info.st_size)
            children.append(node)
        manifest = [(n['name'], n['type'], n['version']) for n in children]
        rel = folder.relative_to(self.docs).as_posix()
        return dict(name=folder.name, path='' if rel == '.' else rel, type='directory',
                    version=version(json.dumps(manifest, ensure_ascii=False).encode('utf-8')),
                    files=sum(n.get('files', 1) for n in children),
                    unsupported=sum(n.get('unsupported', 0) + (1 if n['type'] == 'unsupported' else 0)
                                    for n in children),
                    children=children)

    def get_tree(self):
        with self.guard:
            sync = self._sync()
            return dict(tree=self._snapshot(self.docs), sync=sync)

    def create_directory(self, path):
        with self.guard:
            if not path:
                raise StoreError('不能创建根目录')
            folder = self._path(path, directory=True)
            try:
                folder.mkdir(parents=True, exist_ok=False)
            except FileExistsError:
                raise TargetExists(f'目标已存在，拒绝覆盖: {path}') from None
            return self._written(folder.relative_to(self.docs).as_posix(), index='skip')

    def _entry_version(self, path):
        return self._snapshot(path)['version'] if path.is_dir() else inspect_file(path)['version']

    def move_entry(self, path, target, expected_version):
        with self.guard:
            if not path or not target:
                raise StoreError('不能移动根目录或覆盖根目录')
            source = self._path(path, directory=True)
            is_folder = source.is_dir()
            if not is_folder:
                self._path(path)
            destination = self._path(target, directory=is_folder)
            case_rename = (str(source) != str(destination) and source == destination
                           and source.samefile(destination))
            if (source == destination and not case_rename) or (is_folder and source in destination.parents):
                raise StoreError('不能移动到自身或子目录')
            if destination.exists() and not case_rename:
                raise FileExistsError('目标路径已存在，拒绝覆盖')
            if self._entry_version(source) != expected_version:
                raise StoreError('版本冲突，请刷新后再移动')
            before = source.stat()
            destination.parent.mkdir(parents=True, exist_ok=True)
            self._path(target, directory=is_folder)
            after = source.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise StoreError('版本冲突，请刷新后再移动')
            source.rename(destination)
            try:
                with self.conn:
                    origin = source.relative_to(self.docs).as_posix()
                    target_path = destination.relative_to(self.docs).as_posix()
                    rows = self.conn.execute('SELECT path FROM document_meta').fetchall()
                    origin_key = path_key(origin)
                    for row in rows:
                        prior = row['path']
                        prior_key = path_key(prior)
                        if prior_key == origin_key or (is_folder and prior_key.startswith(origin_key + os.sep)):
                            moved = target_path + prior[len(origin):]
                            self.conn.execute('UPDATE document_meta SET path=?,name=? WHERE path=?',
                                              (moved, Path(moved).name, prior))
                            self.conn.execute('UPDATE chunks SET path=? WHERE path=?', (moved, prior))
            except Exception as exc:
                raise StoreError(f'文件已修改、索引未同步: {target_path}: {exc}') from exc
            return self._written(target_path, index='skip')

    def delete_directory(self, path, expected_version):
        with self.guard:
            if not path:
                raise StoreError('不能删除根目录')
            folder = self._path(path, directory=True)
            snapshot = self._snapshot(folder)
            if snapshot['version'] != expected_version:
                raise StoreError('版本冲突，请刷新后再删除')
            if any(folder.iterdir()):
                raise StoreError('只能删除空文件夹')
            folder.rmdir()
            return self._written(path, index='skip')

    def _check_version(self, file, expected):
        check_link(file)
        info = inspect_file(file)
        if expected != info['version']:
            raise StoreError(f'版本冲突，当前版本: {info["version"]}')
        return info

    @staticmethod
    def _unchanged(file, info):
        current = file.stat()
        return (current.st_size, current.st_mtime_ns) == (info['size'], info['mtime_ns'])

    def _index_one(self, file, rel):
        with self.conn:
            return self._index_file(file, rel)

    def _written(self, path, index='file', **extra):
        if index == 'skip':
            return dict(ok=True, path=path, **extra)
        try:
            if index == 'remove':
                with self.conn:
                    self._remove_index(path)
            else:
                file = self._path(path)
                self._index_one(file, file.relative_to(self.docs).as_posix())
        except Exception as exc:
            raise StoreError(f'文件已修改、索引未同步: {path}: {exc}') from exc
        return dict(ok=True, path=path, **extra)

    def create_document(self, path, content):
        with self.guard:
            file = self._path(path)
            rel = file.relative_to(self.docs).as_posix()
            data = self._bytes(content)
            file.parent.mkdir(parents=True, exist_ok=True)
            self._path(path)
            try:
                with file.open('xb') as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
            except FileExistsError:
                raise TargetExists(f'目标已存在，拒绝覆盖: {rel}') from None
            return self._written(rel, version=version(data))

    def update_document(self, path, content, expected_version):
        with self.guard:
            file = self._path(path)
            info = self._check_version(file, expected_version)
            original, _ = self._load(file)
            data = self._bytes(content, original)
            temp = None
            try:
                with tempfile.NamedTemporaryFile(dir=file.parent, prefix='.update-', delete=False) as stream:
                    temp = Path(stream.name)
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                self._path(path)
                if not self._unchanged(file, info):
                    raise StoreError('版本冲突，请重新读取')
                os.replace(temp, file)
            finally:
                if temp is not None and temp.exists():
                    temp.unlink()
            return self._written(file.relative_to(self.docs).as_posix(), version=version(data))

    def delete_document(self, path, expected_version):
        with self.guard:
            file = self._path(path)
            info = self._check_version(file, expected_version)
            record_id = uuid.uuid4().hex
            folder = self.trash / record_id
            check_link(self.trash)
            folder.mkdir()
            meta = dict(record_id=record_id, path=file.relative_to(self.docs).as_posix(),
                        deleted_at=datetime.now(timezone.utc).isoformat(), version=expected_version)
            try:
                (folder / 'meta.json').write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')
                self._path(path)
                if not self._unchanged(file, info):
                    raise StoreError('版本冲突，请重新读取')
                os.replace(file, folder / 'content')
            except Exception:
                (folder / 'meta.json').unlink(missing_ok=True)
                folder.rmdir()
                raise
            return self._written(meta['path'], index='remove', record_id=record_id)

    def _cleanup_record(self, folder):
        try:
            (folder / 'content').unlink(missing_ok=True)
            (folder / 'meta.json').unlink(missing_ok=True)
            folder.rmdir()
        except OSError:
            pass

    def _records(self):
        check_link(self.trash)
        rows = []
        for folder in self.trash.iterdir():
            if not re.fullmatch('[0-9a-f]{32}', folder.name):
                raise StoreError(f'非法回收记录: {folder.name}')
            check_link(folder)
            meta_file = folder / 'meta.json'
            if not meta_file.exists():
                self._cleanup_record(folder)
                continue
            check_link(meta_file)
            meta = json.loads(meta_file.read_text(encoding='utf-8'))
            if meta.get('restored_at'):
                # The document is already back in place; hide the record and
                # retry cleanup, which may have failed while a handle was open.
                self._cleanup_record(folder)
                continue
            if meta.get('record_id') != folder.name:
                raise StoreError('回收记录 ID 不匹配')
            self._path(meta['path'])
            check_link(folder / 'content')
            rows.append(meta)
        return sorted(rows, key=lambda r: (r['deleted_at'], r['record_id']), reverse=True)

    def list_deleted(self, offset=0, limit=20):
        with self.guard:
            self._page(offset, limit)
            rows = self._records()
            return dict(total=len(rows), items=rows[offset:offset + limit])

    def restore_document(self, record_id):
        with self.guard:
            if not re.fullmatch('[0-9a-f]{32}', record_id):
                raise StoreError('非法回收记录 ID')
            meta = next((r for r in self._records() if r['record_id'] == record_id), None)
            if meta is None:
                raise StoreError('回收记录不存在')
            folder = self.trash / record_id
            info = inspect_file(folder / 'content')
            if info['version'] != meta['version']:
                raise StoreError('回收内容校验失败')
            file = self._path(meta['path'])
            file.parent.mkdir(parents=True, exist_ok=True)
            self._path(meta['path'])
            temp = None
            try:
                with tempfile.NamedTemporaryFile(dir=file.parent, prefix='.restore-', delete=False) as stream:
                    temp = Path(stream.name)
                    with (folder / 'content').open('rb') as source:
                        copy_bytes(source, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                if inspect_file(temp)['version'] != meta['version']:
                    raise StoreError('回收内容复制校验失败')
                try:
                    os.link(temp, file)  # Atomic publication without overwriting a competing target.
                except FileExistsError:
                    raise TargetExists(f'目标已存在，拒绝覆盖: {meta["path"]}') from None
            finally:
                if temp is not None and temp.exists():
                    temp.unlink()
            meta['restored_at'] = datetime.now(timezone.utc).isoformat()
            try:
                (folder / 'meta.json').write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')
            except OSError:
                try:
                    (folder / 'meta.json').unlink()
                except OSError:
                    pass
            try:
                (folder / 'content').unlink()
                (folder / 'meta.json').unlink()
                folder.rmdir()
            except OSError:
                # The document is already back in place; leave the record for
                # opportunistic cleanup on the next list_deleted() call.
                pass
            return self._written(meta['path'], version=info['version'])

    def purge_document(self, record_id):
        with self.guard:
            if not re.fullmatch('[0-9a-f]{32}', record_id):
                raise StoreError('非法回收记录 ID')
            meta = next((r for r in self._records() if r['record_id'] == record_id), None)
            if meta is None:
                raise StoreError('回收记录不存在')
            folder = self.trash / record_id
            self._cleanup_record(folder)
            if folder.exists():
                raise StoreError('回收记录删除失败，文件可能被占用')
            return dict(ok=True, path=meta['path'], record_id=record_id)

    def purge_all_deleted(self):
        with self.guard:
            rows = self._records()
            for meta in rows:
                folder = self.trash / meta['record_id']
                self._cleanup_record(folder)
                if folder.exists():
                    raise StoreError(f'回收记录删除失败，文件可能被占用: {meta["path"]}')
            return dict(ok=True, removed=len(rows))
