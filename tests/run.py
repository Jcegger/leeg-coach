"""Run leeg's tests: unit tests (test_units.py) plus the build-scenario golden file.

    python tests/run.py           # run everything, exit 1 on any failure
    python tests/run.py --update  # accept the current scenario output as the new golden file

The scenarios replay real games through the build pick + every validator. Their output
depends on mundo/shen/kogmaw build.md and live CommunityDragon item data, so after an
intended build.md change (or a patch that renames items) review the diff, then --update.
"""
import difflib
import subprocess
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
GOLDEN = HERE / 'scenarios.expected.txt'


def run_units():
    sys.path.insert(0, str(HERE))
    import test_units
    failed = 0
    tests = [(n, f) for n, f in vars(test_units).items() if n.startswith('test_') and callable(f)]
    for name, fn in tests:
        try:
            fn()
            print(f'  ok    {name}')
        except Exception:
            failed += 1
            print(f'  FAIL  {name}')
            print('        ' + traceback.format_exc().strip().replace('\n', '\n        '))
    print(f'units: {len(tests) - failed}/{len(tests)} passed')
    return failed


def run_scenarios(update):
    out = subprocess.run([sys.executable, str(HERE / 'scenarios.py')], capture_output=True, text=True, cwd=HERE)
    text = out.stdout + (out.stderr if out.returncode else '')
    if out.returncode:
        print('scenarios: crashed\n' + text[-2000:])
        return 1
    if update or not GOLDEN.exists():
        GOLDEN.write_text(text)
        print(f'scenarios: golden file written ({len(text.splitlines())} lines)')
        return 0
    want = GOLDEN.read_text()
    if text == want:
        print('scenarios: match the golden file')
        return 0
    diff = difflib.unified_diff(want.splitlines(), text.splitlines(), 'expected', 'now', lineterm='', n=1)
    print('scenarios: CHANGED (review; if intended, rerun with --update)')
    print('\n'.join(list(diff)[:80]))
    return 1


if __name__ == '__main__':
    update = '--update' in sys.argv
    bad = run_units() + run_scenarios(update)
    sys.exit(1 if bad else 0)
