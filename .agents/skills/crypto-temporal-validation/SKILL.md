---
name: crypto-temporal-validation
description: Review crypto quantitative methods using rolling walk-forward splits, CPCV label purging and embargo, fixed-candidate out-of-sample evaluation, and separate exploratory CSCV/PBO diagnostics. Use for leakage review or backtest validation.
---
# Temporal research validation

1. Require chronologically ordered `LabelWindow` start/end timestamps, aligned candidates, source availability evidence, and a predeclared selection policy.
2. Use `walk_forward_splits` for rolling train/test evaluation; use `cpcv_splits` for combinatorial purged split indices. An embargo is measured in samples, and these functions DO NOT train models.
3. `select_and_evaluate_folds` only evaluates fixed precomputed candidate return streams. Fitted estimators must be separately re-trained on each permitted training fold.
4. `cscv_pbo` is distinct from CPCV. Its rank-based estimate is exploratory, not a calibrated probability of future gains, confidence interval or evidence of a deployable edge.
5. Test overlapping labels, post-cutoff features, skipped observations, incompatible instruments, outcome-dependent selection and insufficient samples. Record actual folds, method versions, dataset hashes, sample coverage and unsupported conclusions.
6. Preserve the independent methodology review and no-trading boundaries.
