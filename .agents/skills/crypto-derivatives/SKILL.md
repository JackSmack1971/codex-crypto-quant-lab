---
name: crypto-derivatives
description: Analyze crypto perpetual basis, funding, open interest, liquidations and positioning across compatible exchange markets while distinguishing USDT-M, coin-M and independent venue products.
---
# Derivatives research
Resolve market as venue:product:symbol:contract and quote/settlement/multiplier. Funding observations are event-based; distinguish current premium/estimated funding from settled funding. Mark-index BPS is not annualized dated-futures carry. OI quantity is not necessarily USD; compute notional only with documented units. Same-venue OI changes require aligned timestamps and identical contract specifications. Cross-venue metrics are context, not silent substitutes. Region-blocked Binance futures is `unavailable`; no VPN/alternate-host bypass. Reject causal liquidation claims from mere price/OI correlation. Flag incomplete liquidation data and uncertainty.
