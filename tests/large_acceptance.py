"""Explicit real-file acceptance; run separately from quick unit tests."""
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from document_stream import MAX_BYTES, BLOCK_BYTES
from knowledge_store import KnowledgeStore, StoreError

PROJECT = Path(__file__).resolve().parents[1]
TEMP = PROJECT / '.test-work'


class MemoryCounters(ctypes.Structure):
    _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD)] + [
        (name, ctypes.c_size_t) for name in ('PeakWorkingSetSize', 'WorkingSetSize',
        'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
        'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage', 'PrivateUsage')]


def memory():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(MemoryCounters), wintypes.DWORD]
    counters = MemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return counters.PrivateUsage, counters.PeakPagefileUsage


def main():
    TEMP.mkdir(exist_ok=True)
    results = []
    with tempfile.TemporaryDirectory(prefix='large-', dir=TEMP) as temp:
        store = KnowledgeStore(Path(temp))
        baseline = memory()[0]

        def step(name, operation):
            start = time.monotonic()
            result = operation()
            private, peak = memory()
            delta = max(0, peak - baseline)
            record = dict(step=name, seconds=round(time.monotonic() - start, 3),
                          private_bytes=private, peak_private_delta=delta)
            results.append(record)
            print(json.dumps(record, ensure_ascii=False), flush=True)
            if delta > 512 * BLOCK_BYTES:
                raise AssertionError(f'峰值 Private Bytes 增量超过 512 MiB: {delta}')
            return result

        file = store.docs / 'large.md'
        positions = [0, MAX_BYTES // 2, MAX_BYTES - 16]
        markers = ['HEADMARK', 'MIDDMARK', 'TAILMARK']

        def generate():
            block = (b'x' * 8191 + b'\n') * 128
            with file.open('wb') as stream:
                for _ in range(MAX_BYTES // len(block)):
                    stream.write(block)
                for position, marker in zip(positions, markers):
                    stream.seek(position)
                    stream.write(marker.encode())
            assert file.stat().st_size == MAX_BYTES

        try:
            step('generate_1.5_GiB', generate)
            report = step('index', store.refresh_index)
            assert report['ok'] and report['added'] == 1, report

            def search():
                for marker, position in zip(markers, positions):
                    found = store.search_documents(marker)
                    assert found['total'] == 1 and found['items'][0]['score'] == 1, found
                    assert found['items'][0]['snippets'][0]['line'] == position // 8192 + 1, found
                assert store.search_documents('HEADMARK MIDDMARK TAILMARK')['total'] == 1

            step('search_head_middle_tail', search)
            for position, marker in zip(positions, markers):
                chunk = step('read_' + marker, lambda: store.read_document_chunk('large.md', position))
                assert chunk['content'].startswith(marker), chunk['content'][:50]
                update = step('patch_' + marker, lambda: store.patch_document(
                    'large.md', position, position + 8, marker.lower(), chunk['version']))
                with file.open('rb') as stream:
                    stream.seek(position)
                    assert stream.read(8) == marker.lower().encode()
                assert file.stat().st_size == MAX_BYTES
            current = update['version']

            def oversized():
                try:
                    store.patch_document('large.md', MAX_BYTES, MAX_BYTES, 'x', current)
                except StoreError as exc:
                    assert '1.5 GiB' in str(exc), exc
                else:
                    raise AssertionError('超限编辑未拒绝')
                with file.open('ab') as stream:
                    stream.write(b'x')
                report = store.refresh_index()
                # 超限文档计 skipped（refresh_index 的 issue(..., True)），不影响整库：
                # ok 仍为 True、failed 为 0，仅该文档不入索引。
                assert report['ok'] and report['skipped'] == 1, report
                assert report['failed'] == 0 and report['updated'] == 0, report
                with file.open('r+b') as stream:
                    stream.truncate(MAX_BYTES)
                assert store.refresh_index()['ok']

            step('reject_over_limit', oversized)
            step('move', lambda: store.move_typed('large.md', 'folder/large.md', current))
            step('rename', lambda: store.rename_entry('folder/large.md', 'renamed.md', current))
            deleted = step('delete', lambda: store.delete_document('folder/renamed.md', current))
            assert store.list_documents()['total'] == 0
            restored = step('restore', lambda: store.restore_document(deleted['record_id']))
            assert restored['version'] == current
            assert (store.docs / 'folder' / 'renamed.md').stat().st_size == MAX_BYTES
            assert store.list_deleted()['total'] == 0
            step('search_after_restore', lambda: search())
        finally:
            store.close()
    report = dict(ok=True, file_bytes=MAX_BYTES, baseline_private=baseline, steps=results,
                  cleaned=True, max_peak_delta=max(r['peak_private_delta'] for r in results))
    (TEMP / 'large_acceptance.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('PASS: real 1.5 GiB acceptance; fixture cleaned', flush=True)


if __name__ == '__main__':
    main()
