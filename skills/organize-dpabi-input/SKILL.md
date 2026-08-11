---
name: organize-dpabi-input
description: Build a read-only preview for copying and naming DICOM, NIfTI, BIDS, or mixed inputs into DPABI staging directories.
---

# Organize DPABI input

1. Scan only the registered source root and preserve source hashes and timestamps.
2. Show every copy and rename as a preview using explicit subject and role mappings.
3. Reject traversal, missing files, duplicate targets, unknown roles, and collisions.
4. Copy to an independent staging workspace only after user confirmation; never move or delete source files.
