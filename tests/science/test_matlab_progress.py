from neuroagent.execution.progress import MatlabProgressObserver


def test_progress_only_uses_actual_allowlisted_messages_and_handles_partial_lines():
    events = []
    observer = MatlabProgressObserver(lambda phase, state: events.append((phase, state)))
    observer.feed("2026 - Running 'Seg")
    observer.feed("ment'\n2026 - Done 'Segment'\n")
    observer.feed("Segment Setup: synthetic-secret-user OK\n")
    observer.feed("Running 'Delete Everything'\nRegressing Out Covariates...\n")
    observer.feed("Regressing Out Covariates finished.\n")
    assert events == [
        ("segmentation", "started"),
        ("segmentation", "finished"),
        ("nuisance", "started"),
        ("nuisance", "finished"),
    ]
    assert "synthetic-secret" not in str(events)


def test_unterminated_log_lines_are_bounded():
    observer = MatlabProgressObserver(lambda *_: None)
    observer.feed("x" * 100_000)
    assert len(observer.buffer) == 4096


def test_observed_spm25_spacing_and_old_segment_messages():
    events = []
    observer = MatlabProgressObserver(lambda phase, state: events.append((phase, state)))
    observer.feed(
        "07-Oct-2026 00:34:04 - Running 'Slice Timing'\n"
        "07-Oct-2026 00:34:49 - Done    'Slice Timing'\n"
        "07-Oct-2026 00:38:19 - Running 'Old Segment'\n"
        "07-Oct-2026 00:40:19 - Done    'Old Segment'\n"
    )
    assert events == [
        ("slice_timing", "started"),
        ("slice_timing", "finished"),
        ("segmentation", "started"),
        ("segmentation", "finished"),
    ]
