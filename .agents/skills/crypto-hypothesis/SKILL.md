---
name: crypto-hypothesis
description: Compare competing crypto market explanations using explicitly assigned, availability-gated observations and reproducible evidence; use for contradictory or uncertain quant theses.
---
# Evidence-based hypothesis competition

1. Start with the user's question, instrument identity, UTC cutoff, horizon and explicit falsifiers. Define competing hypotheses *before* inspecting candidate results when feasible.
2. Invoke existing market, derivatives, on-chain and quality Skills as relevant. Capture independent observations and unique IDs, including source timestamp, retrieval timestamp and (when verifiable) earliest public availability time. A retrieval time is not proof of historical availability.
3. Normalize source evidence into `research_lab.Observation` and calculated methods into `research_lab.ResearchSignal`. No guessed values or scores; positive scores are not probabilities and a favorable raw metric is not automatically bullish.
4. Declare which signals support or contradict each hypothesis and the required metric identities. Use `research_lab.evaluate_hypothesis` for deterministic availability filtering and overlap disclosure. An agent's agreement is not a separate observation.
5. Report supported/contradicted/mixed/insufficient outcomes with untested assumptions and falsifiers. Reconcile incompatible instruments, units and windows outside the aggregator; no implicit conversion, score averaging, or numerical probability.
6. Submit to the existing methodology reviewer and `crypto-report` Skill. A missing essential metric requires `INSUFFICIENT_EVIDENCE` and, where relevant, `NO_TRADE`.

This Skill does not grant tool access, add new data feeds or permit execution of trades. The helper module is an offline reference contract, not an LLM agent or independent review authority.
