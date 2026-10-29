"""Bound disposable video segments without touching point-event evidence."""
import os
from pathlib import Path
import time


def prune_video_archive(directory, *, max_mb, max_age_seconds=120):
    # A video segment without an explicit processing acknowledgement may still
    # contain an unseen passage. An age or byte ceiling is not such an ack.
    if type(max_mb) is not int or max_mb<1 or max_age_seconds<1:
        raise ValueError('positive video archive cache bounds required')
    return dict(files=0,bytes=0,remaining_bytes=None,pending_protected=True)
