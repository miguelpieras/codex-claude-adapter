"""Explicit bounded live smoke using only the logged-in Claude Code subscription."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import time
import uuid

from native import MODEL, NativeRuntime


def main():
    with tempfile.TemporaryDirectory(prefix='claude-adapter-smoke-') as temp:
        root = Path(temp)
        runtime = NativeRuntime(root / 'state')
        threads = []
        for i in range(2):
            cwd = root / ('workspace-' + str(i))
            cwd.mkdir()
            (cwd / 'fixture.txt').write_text('adapter-native-' + str(i))
            tid = str(uuid.uuid4())
            runtime.bind(tid, cwd)
            threads.append((tid, cwd, i))
        spans = []
        def run(entry):
            tid, cwd, i = entry
            events = []
            prompt = ('This is a bounded adapter smoke in an isolated temporary directory. '
                'Read fixture.txt with your native Read tool and write its exact contents '
                'to result.txt using your native Write tool. Do not use Bash or edit anything else. ')
            if i == 0:
                prompt += ('Then use exactly one native Agent subagent to read result.txt and report its contents. '
                           'The subagent must use your same Opus model. Wait for it to finish. ')
            prompt += 'Reply briefly with the fixture contents.'
            start = time.monotonic()
            final, _ = runtime.infer(tid, {'model': MODEL, 'input': [{'role': 'user', 'content': prompt}],
                'reasoning': {'effort': 'low'}}, events.append, lambda: False)
            end = time.monotonic()
            assert (cwd / 'result.txt').read_text().strip() == 'adapter-native-' + str(i)
            assert any('using Read.' in v for v in events), events
            assert any('using Write.' in v for v in events), events
            if i == 0:
                assert any('using Agent.' in v for v in events), events
            spans.append((start, end))
            return {'task': i, 'verified_file': True, 'native_tools': [v for v in events if 'is using' in v],
                    'model': MODEL, 'answer': final, 'seconds': round(end-start, 2)}
        try:
            with ThreadPoolExecutor(2) as pool:
                results = list(pool.map(run, threads))
            overlap = min(v[1] for v in spans) - max(v[0] for v in spans)
            assert overlap > 0
            print(json.dumps({'subscription_only': True, 'parallel_overlap_seconds': round(overlap, 2),
                              'results': results}, indent=2))
        finally:
            runtime.close()


if __name__ == '__main__':
    main()
