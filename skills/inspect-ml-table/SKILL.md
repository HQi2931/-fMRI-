---
name: inspect-ml-table
description: Inspect an uploaded CSV, TSV, or XLSX table for schema quality, missingness, duplicates, leakage, target candidates, and subject alignment.
---

# Inspect ML table

1. Read the upload in staging and report encoding, headers, row count, types, missingness, duplicates, formula-like cells, and high-cardinality columns.
2. Identify candidate target and subject/group columns but require the user to confirm them.
3. Block empty tables, duplicate headers, unsafe formulas, and subject-level leakage.
4. Keep the original upload unchanged and return its content hash.
