---
name: prepare-demographics-template
description: Generate an empty, subject-aligned demographics CSV/XLSX template from a frozen rs-fMRI manifest without inferring participant information.
---

# Prepare demographics template

1. Fill only canonical `subject_id` and `session_id` values from the manifest.
2. Leave group, age, sex, site, handedness, and study-specific fields blank.
3. Mark required fields and return missing-information and alignment reports.
4. Never infer protected or clinical attributes from filenames or images.
