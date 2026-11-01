"""Bounded UTF-8 file processing shared by indexing and editing."""
import codecs
import hashlib
import re

BLOCK_BYTES = 1024 * 1024
FULL_BYTES = 10 * BLOCK_BYTES
MAX_BYTES = 1536 * BLOCK_BYTES
BREAKS = re.compile(r'\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]')


def read_block(stream, limit=BLOCK_BYTES):
    data = stream.read(limit)
    if not data:
        return b''
    # Only incomplete trailing code points may be deferred; invalid UTF-8 fails.
    try:
        data.decode('utf-8')
    except UnicodeDecodeError as exc:
        if exc.reason != 'unexpected end of data' or not stream.peek(1):
            raise
        stream.seek(exc.start - len(data), 1)
        data = data[:exc.start]
    if data.endswith(b'\r') and stream.peek(1)[:1] == b'\n':
        stream.seek(-1, 1)
        data = data[:-1]
    if not data and stream.peek(1):
        raise ValueError('分段长度不足以容纳 UTF-8 字符或 CRLF')
    return data


def file_blocks(path):
    with path.open('rb') as stream:
        if stream.read(3) != codecs.BOM_UTF8:
            stream.seek(0)
        line = 1
        while True:
            offset = stream.tell()
            data = read_block(stream)
            if not data:
                break
            text = data.decode('utf-8')
            yield offset, data, text, line
            line += len(BREAKS.findall(text))


def inspect_file(path, consume=None):
    before = path.stat()
    if before.st_size > MAX_BYTES:
        raise ValueError('文档超过 1.5 GiB')
    with path.open('rb') as stream:
        bom = stream.read(3) == codecs.BOM_UTF8
    digest = hashlib.sha256(codecs.BOM_UTF8 if bom else b'')
    breaks = 0
    last = ''
    newline = '\n'
    size = 3 if bom else 0
    for offset, data, text, line in file_blocks(path):
        digest.update(data)
        size += len(data)
        if size > MAX_BYTES:
            raise ValueError('文档超过 1.5 GiB')
        if '\r\n' in text:
            newline = '\r\n'
        elif '\r' in text and newline != '\r\n':
            newline = '\r'
        breaks += len(BREAKS.findall(text))
        last = text[-1:]
        if consume:
            consume(offset, data, text, line)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or size != after.st_size:
        raise ValueError('扫描期间文件发生变化，请重试')
    return dict(size=size, mtime_ns=after.st_mtime_ns, version=digest.hexdigest(),
                total_lines=breaks + int(bool(last) and not BREAKS.fullmatch(last)),
                bom=bom, newline=newline)


def copy_bytes(source, target, count=None):
    remaining = count
    while remaining is None or remaining:
        data = source.read(BLOCK_BYTES if remaining is None else min(BLOCK_BYTES, remaining))
        if not data:
            if remaining:
                raise ValueError('复制期间文件长度发生变化')
            break
        target.write(data)
        if remaining is not None:
            remaining -= len(data)


def search_chunks(rows, words):
    """Count non-overlapping matches, retaining bounded context across chunks."""
    patterns = [re.compile(re.escape(word)) for word in words]
    # Pure CJK/digit queries need no case folding; skipping it avoids a full copy.
    fold = any(any(char.lower() != char.upper() for char in word) for word in words)
    counts = [0] * len(words)
    next_at = [0] * len(words)
    carry = ''
    base = 0
    line = 1
    snippets = []
    keep = max(max(map(len, words)) + 2, 300)

    def process(text, final):
        nonlocal carry, base, line
        cut = len(text) if final else max(0, len(text) - keep)
        if cut and text[cut - 1:cut + 1] == '\r\n':
            cut -= 1
        lower = text.lower() if fold else text
        low_cut = len(text[:cut].lower()) if fold else cut
        for index, pattern in enumerate(patterns):
            word = words[index]
            start_at = max(0, next_at[index] - base)
            count = lower.count(word, start_at, low_cut)
            # count() excludes matches crossing the cut. Admit the first such
            # match only when it does not overlap the last complete match.
            edge = lower.find(word, max(start_at, low_cut - len(word) + 1))
            if 0 <= edge < low_cut and count and lower.count(word, start_at, edge) < count:
                # Locate the last complete match's end without walking every
                # overlapping occurrence of a long, repetitive keyword.
                left, right = edge + 1, low_cut
                while left < right:
                    middle = (left + right) // 2
                    if lower.count(word, start_at, middle) < count:
                        left = middle + 1
                    else:
                        right = middle
                edge = lower.find(word, left)
            if 0 <= edge < low_cut:
                count += 1
                next_at[index] = base + edge + len(word)
            else:
                next_at[index] = base + max(start_at, low_cut)
            counts[index] += count
            if not count or (len(snippets) == 3 and line >= snippets[-1]['line']):
                continue
            match = pattern.search(lower, start_at)
            while match and match.start() < low_cut:
                if len(snippets) < 3 or line < snippets[-1]['line']:
                    # Map expanding lowercase characters back to source characters.
                    position = match.start()
                    if len(lower) != len(text):
                        length = 0
                        for position, char in enumerate(text):
                            length += len(char.lower())
                            if length > match.start():
                                break
                    number = line + len(BREAKS.findall(text[:position]))
                    if len(snippets) == 3 and number >= snippets[-1]['line']:
                        break
                    stop = BREAKS.search(text, position)
                    next_snippet = ((len(text[:stop.end()].lower()) if fold else stop.end())
                                    if stop else len(lower) + 1)
                    if all(s['line'] != number for s in snippets):
                        start = max(0, position - 60)
                        prefix = list(BREAKS.finditer(text[:position]))
                        if prefix:
                            start = max(start, prefix[-1].end())
                        snippets.append(dict(line=number, text=text[start:min(start + 240, stop.start() if stop else len(text))]))
                        snippets.sort(key=lambda item: item['line'])
                        del snippets[3:]
                    match = pattern.search(lower, max(match.end(), next_snippet))
                else:
                    break
        base += low_cut
        line += len(BREAKS.findall(text[:cut]))
        carry = text[cut:]

    for row in rows:
        process(carry + row['content'], False)
    process(carry, True)
    snippets.sort(key=lambda item: item['line'])
    return counts, snippets[:3]
