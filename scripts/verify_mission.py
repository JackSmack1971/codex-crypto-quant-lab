"""Verify a local research mission bundle.

Exit codes (stable, documented in control-plane/mission-completion.md):
  0  VERIFIED            artifact integrity passed AND evidence-dependent checks were performed
                         (evidence ids resolved against --archive; report companions semantically re-derived).
  1  FAILED              the bundle is incomplete, blocked, invalid, or a performed check failed.
  2  PARTIALLY_VERIFIED  every check that could run passed, but evidence-dependent checks were NOT performed
                         (no/unreadable --archive, or companions not semantically verified, including declared render@1
                         and legacy companion-free bundles). This is NOT verification success. Automated qualification gates must
                         treat any non-zero code as not-passed; ``--allow-partial`` exists only for local use and
                         changes the exit code, never the reported ``verification_status``.
"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research_lab.completion import verify
from research_lab.reporting import verify_bundle_companions

EXIT_VERIFIED, EXIT_FAILED, EXIT_PARTIAL = 0, 1, 2
FULL_COMPANION_STATUSES = ('SEMANTICALLY_VERIFIED',)   # same bar as history: a declared/legacy companion-free bundle is never full success


def classify(result: dict, companions: dict) -> dict:
    """Machine-readable verification level. ``status`` (the completion verdict) is left untouched for compatibility."""
    not_performed = []
    if result.get('layers', {}).get('source_provenance', {}).get('state') != 'ARCHIVE_RESOLVED':
        not_performed.append('evidence_resolution: no readable --archive; evidence ids were not resolved or recomputed')
    if companions['status'] not in FULL_COMPANION_STATUSES + ('FAILED',):
        not_performed.append(f'report_companions: {companions["status"]} (claims not semantically re-derived from the archive)')
    if result['status'] != 'COMPLETE' or companions['status'] == 'FAILED':
        level, code = 'FAILED', EXIT_FAILED
    elif not_performed:
        level, code = 'PARTIALLY_VERIFIED', EXIT_PARTIAL
    else:
        level, code = 'VERIFIED', EXIT_VERIFIED
    return {'verification_status': level, 'exit_code': code,
            'evidence_verification': 'NOT_PERFORMED' if not_performed else 'PERFORMED', 'not_performed': not_performed}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--archive', type=Path, default=None,
                        help='existing EvidenceArchive SQLite file used to resolve evidence ids and recompute metrics')
    parser.add_argument('--allow-partial', action='store_true',
                        help='exit 0 for PARTIALLY_VERIFIED (local convenience only; the JSON status is unchanged and not a verification success)')
    args = parser.parse_args(argv)
    result = verify(args.directory, archive=args.archive)
    # Report companions sit outside the research-mission.v2 artifact table, so the completion gate does not cover them.
    # The same verdict finalization computes is reported here: SEMANTICALLY_VERIFIED needs --archive; a FAILED verdict fails the run.
    companions = verify_bundle_companions(args.directory, args.archive)
    summary = classify(result, companions)
    result = {**result, 'report_companions': companions, **summary}
    print(json.dumps(result, indent=2, sort_keys=True))
    if summary['verification_status'] == 'PARTIALLY_VERIFIED':
        print('PARTIALLY_VERIFIED: ' + '; '.join(summary['not_performed']) + ' -- not a verification success', file=sys.stderr)
        return EXIT_VERIFIED if args.allow_partial else EXIT_PARTIAL
    return summary['exit_code']

if __name__ == '__main__':
    raise SystemExit(main())
