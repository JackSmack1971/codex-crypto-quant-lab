"""Verify a local research mission bundle."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research_lab.completion import verify

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--archive', type=Path, default=None,
                        help='existing EvidenceArchive SQLite file used to resolve evidence ids and recompute metrics')
    args = parser.parse_args(argv)
    result = verify(args.directory, archive=args.archive)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result['status'] == 'COMPLETE' else 1

if __name__ == '__main__':
    raise SystemExit(main())
