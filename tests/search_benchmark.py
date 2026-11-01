"""Reproducible synthetic search timing; not part of unit-test discovery."""
import argparse
import cProfile
import hashlib
import io
import json
from pathlib import Path
import pstats
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from knowledge_store import KnowledgeStore

PROJECT = Path(__file__).resolve().parents[1]
TEMP = PROJECT / '.test-work'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    parser.add_argument('--compare')
    args = parser.parse_args()
    output = (TEMP / args.output).resolve()
    if not output.is_relative_to(TEMP.resolve()):
        parser.error('output must stay within .test-work')
    TEMP.mkdir(exist_ok=True)
    results = {}
    with tempfile.TemporaryDirectory(prefix='search-', dir=TEMP) as temp:
        store = KnowledgeStore(Path(temp))
        try:
            padding = ('common_prefix ordinary text 开发记录 ' + 'x' * 960 + '\n') * 256
            for number in range(96):
                folder = store.docs / ('selected' if number < 24 else 'other')
                folder.mkdir(exist_ok=True)
                extra = '稀有关键词 common_unique_suffix NEEDLE\n' if number % 24 == 0 else ''
                (folder / f'{number:03}.md').write_text(extra + padding, encoding='utf-8')
            (store.docs / 'dense.md').write_text('a' * (4 * 1024 * 1024), encoding='utf-8')
            report = store.refresh_index()
            assert report['ok'], report
            cases = {
                'chinese': dict(query='稀有关键词'),
                'ascii_common_prefix': dict(query='common_unique_suffix'),
                'ascii_rare': dict(query='needle'),
                'short': dict(query='ne'),
                'dense': dict(query='aaa'),
                'multiword': dict(query='开发记录 needle'),
                'directory': dict(query='稀有关键词', directory='selected'),
                'missing': dict(query='完全不存在'),
            }
            print(json.dumps(dict(documents=97, bytes=sum(
                p.stat().st_size for p in store.docs.rglob('*.md')))), flush=True)
            for name, options in cases.items():
                store.search_documents(**options)
                times = []
                for _ in range(3):
                    start = time.perf_counter()
                    result = store.search_documents(**options)
                    times.append(time.perf_counter() - start)
                # Exclude sync timing; compare all returned search fields exactly.
                result.pop('sync')
                digest = hashlib.sha256(json.dumps(result, sort_keys=True,
                    ensure_ascii=False).encode()).hexdigest()
                results[name] = dict(seconds=statistics.median(times),
                                     total=result['total'], digest=digest)
                print(json.dumps(dict(case=name, **results[name])), flush=True)
            profile = cProfile.Profile()
            profile.runcall(store.search_documents, 'aaa')
            stream = io.StringIO()
            pstats.Stats(profile, stream=stream).sort_stats('cumulative').print_stats(12)
            print(stream.getvalue(), flush=True)
        finally:
            store.close()
    if args.compare:
        before = json.loads((TEMP / args.compare).read_text(encoding='utf-8'))
        for name, result in results.items():
            assert result['digest'] == before[name]['digest'], name
            print(f'{name}: {before[name]["seconds"] / result["seconds"]:.2f}x', flush=True)
    output.write_text(json.dumps(results, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
