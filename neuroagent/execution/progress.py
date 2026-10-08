"""Recognize fixed execution messages; never persist raw MATLAB log lines."""

from __future__ import annotations

import re
from collections.abc import Callable

_SPM_PHASES = {
    "Slice Timing": "slice_timing",
    "Realign: Estimate & Reslice": "realignment",
    "Coregister: Estimate": "coregistration",
    "Segment": "segmentation",
    "Old Segment": "segmentation",
    "Normalise: Estimate & Write": "normalization",
    "Normalise: Write": "normalization",
    "Old Normalise: Write": "normalization",
    "Smooth": "smoothing",
}


class MatlabProgressObserver:
    def __init__(self, publish: Callable[[str, str], None]) -> None:
        self.publish = publish
        self.buffer = ""

    def feed(self, text: str) -> None:
        self.buffer += text
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            match = re.search(r"(?:^|\s)(Running|Done)\s+'([^']+)'\s*$", line.strip())
            if match and match[2] in _SPM_PHASES:
                self.publish(
                    _SPM_PHASES[match[2]], "started" if match[1] == "Running" else "finished"
                )
            elif line.strip() in {"Bet begin...", "Bet finished"}:
                self.publish(
                    "brain_extraction", "started" if line.strip() == "Bet begin..." else "finished"
                )
            elif line.strip() == "Regressing Out Covariates...":
                self.publish("nuisance", "started")
            elif line.strip() == "Regressing Out Covariates finished.":
                self.publish("nuisance", "finished")
            elif line.strip() == "Computing ALFF...":
                self.publish("metric_calculation", "started")
            elif re.fullmatch(
                r"ALFF and fALFF compution over, elapsed time: [0-9.]+ seconds\.", line.strip()
            ):
                self.publish("metric_calculation", "finished")
        self.buffer = self.buffer[-4096:]
