"""Bound disposable video segments without touching point-event evidence."""
import os
from pathlib import Path
import time


def prune_video_archive(directory, *, max_mb, max_age_seconds=120):
    root=Path(directory).resolve()
    if type(max_mb) is not int or max_mb<1 or max_age_seconds<1:
        raise ValueError('positive video archive cache bounds required')
    if not root.exists():return dict(files=0,bytes=0,remaining_bytes=0)
    opened=set()
    if Path('/proc').is_dir():
        prefix=str(root)+os.sep
        for process in Path('/proc').iterdir():
            if not process.name.isdigit():continue
            try:
                for fd in (process/'fd').iterdir():
                    try:target=os.readlink(fd)
                    except OSError:continue
                    if target.startswith(prefix):opened.add(target)
            except OSError:continue
    files=[];used=0
    for folder in root.iterdir():
        if folder.is_symlink() or not folder.is_dir():continue
        for path in folder.glob('*.mkv'):
            try:
                if path.is_symlink():continue
                stat=path.stat();files.append((stat.st_mtime,path,stat.st_size));used+=stat.st_size
            except FileNotFoundError:pass
    now=time.time();removed=removed_bytes=0
    for modified,path,size in sorted(files):
        if modified>=now-max_age_seconds and used<=max_mb*1024*1024:break
        # Unix unlink would otherwise conceal a still-growing open segment.
        if str(path) in opened or modified>=now-45:continue
        if not path.resolve().is_relative_to(root):raise ValueError('archive cache escaped its root')
        try:path.unlink()
        except FileNotFoundError:pass
        except PermissionError:continue
        removed+=1;removed_bytes+=size;used-=size
    return dict(files=removed,bytes=removed_bytes,remaining_bytes=used,
        target_bytes=max_mb*1024*1024,active_segments_protected=True)
