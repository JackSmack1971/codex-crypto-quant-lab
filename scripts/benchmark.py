"""Run the deterministic offline benchmark suite (engineering correctness only; never predictive performance).

python scripts/benchmark.py [--cases B01,B02] [--repeat N] [--output result.json]

Exit codes: 0 = no FAIL anywhere, repeat runs identical and (full run) every mandatory dimension evaluated; 1 = at least one FAIL (or non-reproducible output);
2 = invalid suite, case or fixture. NOT_EVALUABLE checks are reported, never counted as PASS.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research_lab.benchmarks import BenchmarkError, repeat_suite, run_suite  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--cases', help='comma-separated case ids (default: all)')
    parser.add_argument('--repeat', type=int, default=1, help='run the suite N times from clean workspaces and compare canonical digests')
    parser.add_argument('--output', type=Path, help='write the JSON result here (never overwrites)')
    args = parser.parse_args(argv)
    only = args.cases.split(',') if args.cases else None
    try:
        if args.repeat > 1:
            outcome = repeat_suite(ROOT, args.repeat, only)
            result, reproducible = outcome['result'], outcome['identical']
            result = {**result, 'repeatability': {k: outcome[k] for k in ('runs', 'canonical_digests', 'identical', 'intentionally_variable')}}
        else:
            result, reproducible = run_suite(ROOT, only), True
        if args.output:
            if args.output.exists():
                raise BenchmarkError('OUTPUT_EXISTS', str(args.output))
            args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    except (BenchmarkError, OSError) as exc:
        print(json.dumps({'error': str(exc)}), file=sys.stderr)
        return 2
    scoring = result['scoring']
    print(json.dumps({'canonical_digest': result['canonical_digest'], 'artifact_digest': result['artifact_digest'],
                      'dimensions_with_no_evaluable_case': scoring['dimensions_with_no_evaluable_case'], 'per_dimension': scoring['per_dimension'],
                      'mandatory_gate': scoring['mandatory_gate'], 'failures': scoring['failures'],
                      'reproducible': reproducible, 'predictive_performance': 'NOT_MEASURED'}, indent=2, sort_keys=True))
    incomplete = scoring['mandatory_gate']['state'] == 'INCOMPLETE' and not args.cases   # a full run must evaluate every mandatory dimension
    return 0 if not scoring['failures'] and reproducible and not incomplete else 1


if __name__ == '__main__':
    raise SystemExit(main())
