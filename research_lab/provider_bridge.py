"""Explicit read-only Python MCP adapter bridge; no arbitrary tool dispatch.

The existing MCP methods are synchronous read-only GET wrappers. This bridge
converts their returned envelopes, never claiming a provider verified historical
publication time. Network calls occur only when collect_observation is invoked.
"""
from __future__ import annotations
from typing import Any
from .core import ContractError, Observation
from .normalization import normalize_envelope

# Canonical local adapter names (not external MCP names, no generic URL input).
READ_ONLY_ADAPTERS: dict[str, str] = {
    'spot_book':'spot_book', 'spot_ticker':'spot_ticker',
    'spot_trades':'spot_trades','spot_candles':'spot_candles',
    'futures_snapshot':'futures_snapshot','futures_oi':'futures_oi',
    'futures_funding_history':'futures_funding_history',
    'defi_protocol':'defi_protocol','defi_chain':'defi_chain',
    'dex_pair':'dex_pair','dex_token_pairs':'dex_token_pairs',
    'btc_mempool':'btc_mempool','btc_fees':'btc_fees',
    'btc_tip':'btc_tip','btc_hashrate':'btc_hashrate',
}


def collect_observation(tool: str, args: dict[str,Any], *,
                        field: str|None=None, field_unit: str|None=None,
                        available_at_utc: str|None=None,
                        availability_basis: str|None=None) -> Observation:
    if tool not in READ_ONLY_ADAPTERS:
        raise ContractError('unknown or unauthorized adapter route')
    if not isinstance(args,dict):
        raise ContractError('adapter args must be mapping')
    from quant_free_mcp import adapters
    fn=getattr(adapters,READ_ONLY_ADAPTERS[tool])
    # Argument validation happens in the adapter; no arbitrary callable names.
    return normalize_envelope(fn(**args),field=field,field_unit=field_unit,
                              available_at_utc=available_at_utc,
                              availability_basis=availability_basis)
