# OpenNeuro 20-person resting-state fMRI demo

- 状态：Implementing
- 文档类型：Operations
- 维护者：system_architect
- 更新日期：2026-10-21
- 关联：[数据集输入与只读契约](../domain/dataset-contract.md)、[模型数据外发策略](../security/data-egress.md)

This reproducible demo uses the public, CC0 BIDS snapshot [OpenNeuro ds002748 v1.0.5](https://openneuro.org/datasets/ds002748/versions/1.0.5), DOI `10.18112/openneuro.ds002748.v1.0.5`. The downloader pins that snapshot rather than following a moving dataset version.

The balanced subset contains 10 participants in the source `depr` group (`sub-21`–`sub-29` and `sub-31`) and 10 in `control` (`sub-52`, `sub-53`, `sub-55`, `sub-56`, `sub-57`, `sub-58`, `sub-60`, `sub-62`, `sub-68`, and `sub-69`). Each participant has one T1w image and one `task-rest` BOLD image. The selected depression entries carry the source diagnoses F32.0 or F32.1. The downloader retains only the selected rows in `participants.tsv` and records the source table hash and all source file URLs in `demo-provenance.json`.

Download the dataset into the ignored local `data/` directory with:

```powershell
uv run python scripts/download-rsfmri-demo.py
```

The default destination is `data/rsfmri-demo/ds002748-v1.0.5`. To inspect the pinned file list and estimated size first, add `--dry-run`. An existing verified download is checked and reused; an unverified or modified destination is never overwritten. The final directory is published only after all files pass the source byte-size checks. `demo-manifest.json` records the local file sizes and SHA-256 values so the completed download can be independently verified.

The local Workbench scanner should identify the output as BIDS and report 20 participants, 20 functional participants, 20 anatomical participants, and 40 NIfTI files. The rest sidecar declares TR = 2.5 seconds and the BOLD headers contain 100 volumes, or about 4 minutes 10 seconds per scan. This is suitable for validating dataset discovery, demographics alignment, workflow preparation, and execution plumbing. The short acquisition is a software demo and does not support treating a group result as a definitive scientific finding.

For local demographics registration, map `participant_id` to the subject ID and `group` to the analysis field `group`. Keep diagnosis and other source columns unmapped unless the study explicitly needs them. The registration endpoint reads the file locally and binds its immutable revision to the frozen subject manifest. Do not add participant-level table contents to remote model context when only preprocessing or general workflow planning is needed.

OpenNeuro documents public dataset access in its [user guide](https://docs.openneuro.org/user_guide.html) and the CC0 policy for new dataset versions in its [FAQ](https://docs.openneuro.org/faq).
