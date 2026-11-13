"""Opt-in real DPABI check of the template's estimated GRF smoothness evidence.

Uses a derivative of an existing authorized synthetic T map, with only its
smoothness description removed. The original map and installation stay read-only.
This verifies the DPABI/adapter branch, rather than a second public Worker run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
from pathlib import Path

import nibabel as nib
import numpy as np
from scipy import ndimage, stats

from neuroagent.application.settings import Settings
from neuroagent.infrastructure.environment import SettingsEnvironmentLockProvider


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def quoted(path: Path) -> str:
    return path.as_posix().replace("'", "''")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--real-matlab", action="store_true")
    args = parser.parse_args()
    if not args.real_matlab:
        parser.error("--real-matlab is required for this synthetic acceptance")
    source = args.source_root.resolve()
    proof = json.loads((source / "summary.json").read_text(encoding="utf-8"))
    assert proof["status"] == "pass" and proof["test"] == "one_sample_t"
    maps = list((source / "work").rglob("statistic.nii"))
    assert len(maps) == 1
    original = maps[0]
    mask = source / "source" / "Mask.nii"
    bootstrap = original.parent.parent / "scripts" / "bootstrap.m"
    before = {str(path): digest(path) for path in (original, mask, bootstrap)}
    root = args.output_root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    image = nib.load(original)
    header = image.header.copy()
    header["descrip"] = b"T{11} authorized synthetic derivative without smoothness"
    derivative = root / "statistic-no-smoothness.nii"
    nib.save(nib.Nifti1Image(np.asarray(image.dataobj), image.affine, header), derivative)
    assert np.array_equal(nib.load(derivative).get_fdata(), image.get_fdata())
    assert np.array_equal(nib.load(derivative).affine, image.affine)
    template = Path("matlab/templates/run_statistics.m.tmpl").read_text(encoding="utf-8")
    helper = template.split("function evidence = grf_smoothness_evidence", 1)[1].split(
        "function evidence = runtime_software_evidence", 1
    )[0]
    (root / "grf_smoothness_evidence.m").write_text(
        "function evidence = grf_smoothness_evidence" + helper, encoding="utf-8"
    )
    script = root / "verify_estimated.m"
    z_before = root / "Z_BeforeThreshold_direct.nii"
    script.write_text(
        f"run('{quoted(bootstrap)}'); addpath('{quoted(root)}');\n"
        f"[~,~,h]=y_ReadRPI('{quoted(derivative)}'); s=w_ReadFWHM(h); assert(s.dLh==0);\n"
        f"[z,minimum,out_header]=y_GRF_Threshold('{quoted(derivative)}',0.001,1,0.05,"
        f"'{quoted(root / 'direct.nii')}','{quoted(mask)}','T',11,[],[],[],[]);\n"
        f"y_Write(z,out_header,'{quoted(root / 'corrected_z.nii')}');\n"
        f"[dlh,~,fwhm,nvox]=y_Smoothest('{quoted(z_before)}','{quoted(mask)}');\n"
        f"p=struct('mask_path','{quoted(mask)}','smoothness_mode','auto','smoothness_dlh',[]);\n"
        f"e=grf_smoothness_evidence('{quoted(derivative)}','{quoted(z_before)}',p);\n"
        "assert(strcmp(e.source,'estimated')); assert(e.dlh==dlh);\n"
        "assert(isequal(e.fwhm_mm,fwhm)); assert(e.mask_voxel_count==nvox);\n"
        f"fid=fopen('{quoted(root / 'evidence.json')}','w');\n"
        "fprintf(fid,'%s',jsonencode(struct('source',e.source,'dlh',dlh,'fwhm',fwhm,"
        "'mask_voxel_count',nvox,'minimum_cluster_size',minimum))); fclose(fid); exit(0);\n",
        encoding="utf-8",
    )
    configuration = SettingsEnvironmentLockProvider(Settings.from_env()).configuration()
    with (
        (root / "stdout.log").open("w", encoding="utf-8") as stdout,
        (root / "stderr.log").open("w", encoding="utf-8") as stderr,
    ):
        try:
            process = subprocess.run(
                [str(configuration.matlab_executable), "-batch", f"run('{quoted(script)}');"],
                stdout=stdout,
                stderr=stderr,
                timeout=180,
                check=False,
            )
        except subprocess.TimeoutExpired:
            (root / "summary.json").write_text(
                json.dumps({"status": "failed", "reason": "matlab_exit_timeout"}), encoding="utf-8"
            )
            raise SystemExit(
                "MATLAB timed out; retained evidence is not a passed acceptance."
            ) from None
    assert process.returncode == 0, (
        "Inspect retained local MATLAB logs for estimated branch failure."
    )
    evidence = json.loads((root / "evidence.json").read_text(encoding="utf-8"))
    nvox, dlh = evidence["mask_voxel_count"], evidence["dlh"]
    assert nvox == int(np.count_nonzero(nib.load(mask).get_fdata())) and math.isfinite(dlh)
    threshold = float(stats.norm.isf(0.001 / 2))
    em = nvox * (2 * math.pi) ** -2 * dlh * (threshold**2 - 1) * math.exp(-(threshold**2) / 2)
    en = nvox * float(stats.norm.sf(threshold))
    beta = (math.gamma(2.5) * em / en) ** (2 / 3)
    minimum = 1
    while -math.expm1(-em * math.exp(-beta * minimum ** (2 / 3))) >= 0.05 / 2:
        minimum += 1
        assert minimum < 1_000_000
    assert minimum == evidence["minimum_cluster_size"]
    raw = nib.as_closest_canonical(nib.load(derivative)).get_fdata()
    masked = nib.as_closest_canonical(nib.load(mask)).get_fdata() != 0
    independent_z = stats.norm.isf(stats.t.sf(np.abs(raw), 11)) * np.sign(raw)
    keep = np.zeros(raw.shape, dtype=bool)
    for sign in (1, -1):
        components, _ = ndimage.label(
            masked & (sign * independent_z >= threshold), structure=np.ones((3, 3, 3))
        )
        sizes = np.bincount(components.ravel())
        accepted = np.flatnonzero(sizes >= minimum)
        keep |= np.isin(components, accepted[accepted != 0])
    actual = nib.as_closest_canonical(nib.load(root / "corrected_z.nii")).get_fdata()
    assert np.array_equal(actual != 0, keep)
    assert all(digest(Path(path)) == value for path, value in before.items())
    result = {
        "status": "pass",
        "mode": "real_matlab_synthetic_estimated_branch",
        **evidence,
        "template_helper_identical_to_direct_smoothest": True,
        "independent_grf_cluster_threshold_and_26_neighbor_support_verified": True,
        "source_files_unchanged": True,
        "limitations": [
            "DPABI/adapter branch verification; does not exercise a public Worker run."
        ],
    }
    (root / "summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({"status": "pass", "smoothness_source": "estimated", "minimum": minimum}))


if __name__ == "__main__":
    main()
