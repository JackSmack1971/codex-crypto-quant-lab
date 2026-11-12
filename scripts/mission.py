"""Local, offline research-mission coordinator CLI (no network, no scheduler, no agents).

python scripts/mission.py --workspace DIR create spec.json
python scripts/mission.py --workspace DIR status MISSION_ID
python scripts/mission.py --workspace DIR advance MISSION_ID [--all]
python scripts/mission.py --workspace DIR resume MISSION_ID     # reconcile_mission, then advance
python scripts/mission.py --workspace DIR cancel MISSION_ID

Exit codes: 0 = request succeeded and the mission is not blocked/failed; 1 = mission blocked, failed,
cancelled or awaiting reconciliation; 2 = invalid request (bad spec, unknown mission, unsafe path).
Job state is execution lifecycle only: COMPLETED never means a hypothesis was validated.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research_lab.core import ContractError  # noqa: E402
from research_lab.mission_coordinator import MissionCoordinator  # noqa: E402
from research_lab.mission_spec import MissionError  # noqa: E402

BAD_OUTCOMES = {'BLOCKED', 'FAILED', 'TERMINAL', 'RECONCILIATION_REQUIRED', 'CLAIM_LOST', 'FENCED', 'CANCELLED'}


def run(argv=None) -> tuple[dict, int]:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--workspace', type=Path, required=True, help='directory holding mission.sqlite, inputs and outputs')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('create').add_argument('spec', type=Path)
    for name in ('status', 'resume', 'cancel'):
        sub.add_parser(name).add_argument('mission_id')
    adv = sub.add_parser('advance')
    adv.add_argument('mission_id')
    adv.add_argument('--all', action='store_true', help='keep advancing until terminal, blocked or no progress')
    args = parser.parse_args(argv)
    try:
        coordinator = MissionCoordinator(args.workspace)
        if args.command == 'create':
            result = coordinator.create_mission(json.loads(args.spec.read_text(encoding='utf-8')))
            return result, 0
        if args.command == 'status':
            result = coordinator.inspect_mission(args.mission_id)
            return result, 0 if result['job']['state'] not in ('FAILED', 'BLOCKED', 'CANCELLED') else 1
        if args.command == 'cancel':
            return coordinator.cancel_mission(args.mission_id), 0
        if args.command == 'resume':
            recovery = coordinator.reconcile_mission(args.mission_id)
            if recovery['outcome'] in ('BLOCKED', 'FAILED', 'FENCED'):
                return {'reconcile': recovery}, 1
            result = coordinator.advance_mission(args.mission_id)
            return {'reconcile': recovery, 'advance': result}, 1 if result['outcome'] in BAD_OUTCOMES else 0
        results = []
        while True:
            result = coordinator.advance_mission(args.mission_id)
            results.append(result)
            if not args.all or result['outcome'] != 'ADVANCED':
                break
        last = results[-1]
        out = last if len(results) == 1 else {'steps': results, 'final': last}
        done = last['outcome'] == 'COMPLETED' or last['outcome'] == 'ADVANCED'
        return out, 0 if done else 1
    except MissionError as exc:
        return {'error': {'code': exc.code, 'message': exc.message, 'diagnostics': exc.diagnostics}}, 2
    except sqlite3.Error as exc:
        return {'error': {'code': 'STORE_UNAVAILABLE', 'message': f'{type(exc).__name__}: mission store unreadable or locked'}}, 2
    except (ContractError, OSError, ValueError) as exc:
        return {'error': {'code': 'INVALID_REQUEST', 'message': f'{type(exc).__name__}: {exc}'}}, 2


if __name__ == '__main__':
    payload, code = run()
    print(json.dumps(payload, indent=2, sort_keys=True))
    raise SystemExit(code)
