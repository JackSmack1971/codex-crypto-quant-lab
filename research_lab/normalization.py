"""Strict offline mapping from existing read-only MCP envelopes to observations.

Only scalar measurements are normalized; arrays/compound values require an
explicit exact field selection. Provider observation time is not release time.
"""
from __future__ import annotations
from typing import Any
from .core import ContractError, Observation, _decimal, _utc, digest


def normalize_envelope(envelope: dict[str, Any], *, field: str | None = None, field_unit: str | None = None,
                       available_at_utc: str | None = None,
                       availability_basis: str | None = None) -> Observation:
    """Normalize a single observed scalar with explicitly substantiated availability.

    For historical eligibility, the caller must separately establish the
    earliest release time; retrieval timestamp cannot substitute for it.
    Unknown availability is valid archival evidence, but ineligible for replay.
    """
    if not isinstance(envelope, dict) or envelope.get('schema_version') != '1.0':
        raise ContractError('unsupported MCP envelope schema')
    for key in ('metric', 'instrument_id', 'retrieved_at_utc'):
        if not isinstance(envelope.get(key), str) or not envelope[key]:
            raise ContractError('missing MCP envelope '+key)
    if available_at_utc is not None:
        _utc(available_at_utc)
        if not availability_basis or not availability_basis.strip():
            raise ContractError('availability requires documented basis')
    if availability_basis and available_at_utc is None:
        raise ContractError('availability basis without timestamp')
    if field is None and field_unit is not None:
        raise ContractError('field_unit requires a field selection')
    source = envelope.get('source')
    if envelope.get('status') != 'ok':
        if envelope.get('value') is not None:
            raise ContractError('unavailable envelope has value')
        return Observation('mcp:'+digest(envelope), envelope['instrument_id'],
                           envelope['metric'], None, envelope.get('unit') or 'unknown',
                           'unavailable:provider', None, envelope['retrieved_at_utc'],
                           None, status='UNAVAILABLE', method='mcp-unavailable')
    if not isinstance(source, dict) or not isinstance(source.get('origin'), str) or not isinstance(source.get('endpoint'), str):
        raise ContractError('missing verified MCP source')
    if not source['origin'].startswith('https://') or not source['endpoint'].startswith('/'):
        raise ContractError('invalid source route')
    if not isinstance(envelope.get('unit'), str) or not envelope['unit']:
        raise ContractError('missing units')
    value = envelope.get('value')
    metric = envelope['metric']
    unit = envelope['unit']
    if field is not None:
        if not isinstance(value, dict) or field not in value or not field or '/' in field:
            raise ContractError('field must match an explicit scalar key')
        value = value[field]
        metric += '.' + field
        # Composite envelopes have mixed units. A field-specific unit is required.
        if not field_unit or not isinstance(field_unit,str):
            raise ContractError('field-specific unit required for composite envelope')
        unit = field_unit
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ContractError('MCP value is not a scalar; select and validate a field')
    number = str(_decimal(str(value)))
    timestamp = envelope.get('observation_time_utc')
    if not isinstance(timestamp, str) or not timestamp:
        # Do not manufacture provider time from retrieval time.
        raise ContractError('source observation time unavailable')
    _utc(timestamp)
    if available_at_utc is not None and _utc(available_at_utc) < _utc(timestamp):
        raise ContractError('availability precedes observation')
    return Observation(id='mcp:'+digest({'raw':envelope,'field':field,'availability':available_at_utc,
                                         'basis':availability_basis,'field_unit':field_unit}),
                       instrument_id=envelope['instrument_id'], metric=metric, value=number,
                       unit=unit, source_uri=source['origin']+source['endpoint'],
                       observed_at_utc=timestamp, retrieved_at_utc=envelope['retrieved_at_utc'],
                       available_at_utc=available_at_utc,
                       method=(envelope.get('methodology') or 'mcp') +
                              (f'; availability:{availability_basis}' if availability_basis else ''))


def normalize_mcp_response(value: dict[str, Any], **kwargs: Any) -> Observation:
    """Alias for an already decoded MCP tool result (not the transport wrapper)."""
    return normalize_envelope(value, **kwargs)
