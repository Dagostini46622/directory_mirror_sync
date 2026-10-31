# directory_mirror_sync

A small Python library that mirrors a source directory onto a destination directory: new and changed files are copied across, and files no longer present in the source are removed from the destination.

```python
from directory_mirror_sync import MirrorSync, SyncResult

result: SyncResult = MirrorSync("./src", "./dst").run()
print(result.copied)   # list of relative paths newly created
print(result.updated)  # list of relative paths whose contents or mode changed
print(result.deleted)  # list of relative paths removed from the destination
print(result.ok)       # True when no errors were recorded
```

## Why this exists

Backups and deploy pipelines usually want a *mirror*, not a one-way copy: if a file is removed upstream it should disappear downstream too. Doing this with `rsync` is fine until you are in an environment where you cannot shell out to it, or where you want the operation expressed as an ordinary Python call so it can be composed with other logic. This library does that one job in pure Python.

The trade-off is simplicity over performance. There is no incremental state file, no rolling checksums, and no background watcher. Each call walks both trees in full and, for files whose sizes match, compares their bytes. That is deliberately dumb: it is easy to reason about and has no failure mode involving a stale state database. For trees in the low tens of thousands of files this is fast enough; if you need to mirror millions of files across a network you want a different tool.

## The awkward edge

A content-identical file whose Unix mode bits differ (for example, the source gained the executable bit) is reported in `updated` and its mode is rewritten on the destination. This is a deliberate choice: a mirror that silently ignores permission changes is not really a mirror. If you only care about contents, filter on `copied` and ignore `updated` entries where you know the content was unchanged.

`SyncResult.errors` collects per-file failures (for example, a source file that cannot be read) as strings rather than raising, so one bad file does not abort the whole pass. Inspect `result.ok` afterwards.

## Design notes

The window stores values eagerly rather than keeping running aggregates. Running
sums drift with floating point over long streams, and recomputing from a small
buffer is cheap enough that the drift is not worth the speed.

## Performance

The window keeps a bounded buffer, so `push` is constant time and memory does not
grow with the length of the stream. `peak` and `trough` are linear in the window
size, which is the trade that keeps `push` cheap.

