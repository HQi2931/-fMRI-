---
name: localize-statistical-clusters
description: Parse DPABI cluster tables and match peak coordinates to a user-supplied atlas without guessing regions or making clinical claims.
---

# Localize statistical clusters

1. Validate cluster columns, coordinate space, statistic, and voxel count.
2. Require an atlas NIfTI plus label table or coordinate-label source for region names.
3. Return distance and confidence for each match; without an atlas report coordinates only.
4. Preserve statistic-map, cluster-table, atlas, and label hashes in the report.
