---
name: diagnose-dpabi-failure
description: Classify a failed MATLAB or DPABI run from a bounded log excerpt and propose reviewable remediation steps without rerunning or modifying data.
---

# Diagnose DPABI failure

1. Read the immutable run attempt, environment lock, input manifest hash, and bounded log excerpt.
2. Classify the failure with deterministic rules before asking an Agent to explain it.
3. Return evidence, confidence, severity, and a structured remediation proposal.
4. Require a new plan revision when parameters, inputs, environment, timeout, or subject inclusion would change.
5. Never execute a command, edit source data, exclude subjects, or restart MATLAB automatically.
