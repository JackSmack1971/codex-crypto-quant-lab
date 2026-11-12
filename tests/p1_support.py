"""Shared deterministic fixtures for P1 mission/registry tests. Offline; no network; no wall clock."""
from __future__ import annotations

import hashlib
import json
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from research_lab.mission_coordinator import MissionCoordinator

AS_OF = '2026-01-01T12:00:00Z'
INSTRUMENT = 'binance:spot:BTCUSDT'
METRIC = 'spot.quote.mid'
REVIEWERS = ('methodology_reviewer', 'source_verifier')


class SimulatedCrash(BaseException):
    """Models process death: not an Exception, so no operation error handling runs."""


class StepClock:
    """Monotonic, thread-safe, deterministic UTC clock (one second per call)."""

    def __init__(self, start='2026-02-01T00:00:00Z'):
        self._t = datetime.fromisoformat(start.replace('Z', '+00:00'))
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            self._t += timedelta(seconds=1)
            return self._t.isoformat(timespec='seconds').replace('+00:00', 'Z')


def ts(hours=10, minutes=0):
    return (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=hours, minutes=minutes)).isoformat().replace('+00:00', 'Z')


def obs_row(id='o1', value='100', *, unit='USDT', instrument=INSTRUMENT, metric=METRIC, observed=None,
            available=None, retrieved=None, source='https://data-api.binance.vision/api/v3/ticker/bookTicker'):
    observed = observed or ts(10, 0)
    return {'id': id, 'instrument_id': instrument, 'metric': metric, 'value': value, 'unit': unit,
            'source_uri': source, 'observed_at_utc': observed, 'retrieved_at_utc': retrieved or ts(10, 1),
            'available_at_utc': available or ts(10, 1), 'status': 'OBSERVED', 'method': 'provider-reported'}


def signal_row(id='s1', obs=('o1',), value='0.5'):
    return {'id': id, 'model_id': 'm1', 'instrument_id': INSTRUMENT, 'horizon': '1d', 'as_of_utc': ts(11, 0),
            'value': value, 'observation_ids': list(obs), 'method_version': 'v1', 'interpretation': 'fixture',
            'status': 'CALCULATED'}


class Fixture:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.clock = StepClock()

    def close(self):
        self.tmp.cleanup()

    def put(self, rel: str, obj) -> dict:
        path = self.ws / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(obj, sort_keys=True).encode()
        path.write_bytes(data)
        return {'path': rel, 'sha256': hashlib.sha256(data).hexdigest()}

    def handoff(self, name, state='PASS', evidence=('o1',), rel=None):
        return self.put(rel or f'inputs/{name}.json', {'schema': 'reviewer-handoff.v1', 'reviewer': name, 'state': state,
                                                       'evidence': list(evidence), 'produced_by': 'test-fixture'})

    def spec(self, mission_id='m1', *, observations=None, signals=None, hypotheses='default', handoffs='both',
             provider_ops=(), attempts=None, accepted=('NOT_EVALUABLE', 'INSUFFICIENT_EVIDENCE', 'EVALUABLE'),
             required_metrics=None, max_age=7200, extra=None, **over):
        observations = [obs_row()] if observations is None else observations
        signals = [signal_row()] if signals is None else signals
        run = self.put(f'inputs/{mission_id}/run.json', {'schema': 'research-run-input.v1', 'run_id': mission_id,
                                                          'as_of_utc': AS_OF, 'observations': observations, 'signals': signals})
        inputs = {'research_run': run}
        if attempts is not None:
            inputs['provider_attempts'] = self.put(f'inputs/{mission_id}/attempts.json', {'attempts': attempts})
        if handoffs == 'both':
            inputs['reviewer_handoffs'] = {n: self.handoff(n, rel=f'inputs/{mission_id}/{n}.json') for n in REVIEWERS}
        elif isinstance(handoffs, dict):
            inputs['reviewer_handoffs'] = handoffs
        hyps = ([{'id': 'h1', 'supporting_signal_ids': ['s1'], 'contradicting_signal_ids': [], 'required_metrics': [METRIC]}]
                if hypotheses == 'default' else hypotheses)
        spec = {
            'schema': 'research-mission-execution.v1', 'mission_id': mission_id,
            'decision_question': 'Is the BTCUSDT mid observation set eligible for a research assessment?',
            'as_of_utc': AS_OF, 'research_horizon': '1d', 'instruments': [INSTRUMENT],
            'required_metrics': required_metrics or [{'instrument_id': INSTRUMENT, 'metric': METRIC, 'unit': 'USDT'}],
            'max_age_seconds': max_age,
            'provider_operations': [{'operation_id': o, 'required': True} for o in provider_ops],
            'hypotheses': hyps,
            'permitted_operations': ['plan.freeze_spec', 'acquire.local_inputs', 'reconcile.sources', 'analyze.hypotheses',
                                     'verify.stage_artifacts', 'render.local_report', 'finalize.bundle'],
            'inputs': inputs, 'output_dir': f'missions/{mission_id}',
            'completion_requirements': {'accepted_research_states': list(accepted), 'required_reviews': list(REVIEWERS)},
        }
        if not hyps:
            spec['no_hypotheses_reason'] = 'descriptive evidence assessment only'
        spec.update(extra or {})
        spec.update(over)
        return spec

    def coordinator(self, **kw):
        kw.setdefault('clock', self.clock)
        return MissionCoordinator(self.ws, **kw)


def run_to_end(coordinator, mission_id, limit=12):
    """Advance until the job is terminal or no progress is possible; returns the list of results."""
    results = []
    for _ in range(limit):
        result = coordinator.advance_mission(mission_id)
        results.append(result)
        if result['job_state'] in ('COMPLETED', 'FAILED', 'BLOCKED', 'CANCELLED') or result['outcome'] not in ('ADVANCED',):
            break
    return results
