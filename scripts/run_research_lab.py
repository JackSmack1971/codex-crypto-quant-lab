#!/usr/bin/env python3
"""CLI: offline historical scenario from validated JSON; no network requests."""
from pathlib import Path
import argparse
import json
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from research_lab.workflows import execute_research_run


def main():
    parser=argparse.ArgumentParser(description='Offline crypto research simulation only')
    parser.add_argument('input',type=Path,help='JSON input following research-run-input.v1')
    parser.add_argument('--archive',type=Path,required=True,help='SQLite evidence archive')
    parser.add_argument('--ledger',type=Path,required=True,help='SQLite research ledger')
    parser.add_argument('--out',type=Path,required=True,help='local result JSON')
    args=parser.parse_args()
    if args.out.resolve() in (args.archive.resolve(),args.ledger.resolve(),args.input.resolve()):
        parser.error('result path must not overwrite inputs or databases')
    if args.out.exists():
        parser.error('result output already exists; choose a new run target')
    try:
        result=execute_research_run(json.loads(args.input.read_text(encoding='utf-8')),
                                    archive_path=args.archive,ledger_path=args.ledger)
        args.out.parent.mkdir(parents=True,exist_ok=True)
        with args.out.open('x',encoding='utf-8') as file:
            file.write(json.dumps(result,sort_keys=True,indent=2)+'\n')
    except (OSError,ValueError,KeyError,TypeError) as exc:
        parser.exit(1,f'research run failed: {exc}\n')
    print(json.dumps({'status':'recorded','run_id':result['result']['run_id'],
                      'snapshot':result['result']['evidence_snapshot_id'],
                      'ledger_tip_hash':result['ledger_tip_hash']}))

if __name__=='__main__':main()
