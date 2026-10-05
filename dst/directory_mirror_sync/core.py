import os
import shutil
import stat
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

__all__ = ["MirrorSync", "SyncResult"]


def _relative_tree(root: str) -> dict:
    """Walk *root* and return a mapping of relative paths to stat info.

    We key on a normalized relative path so that the same logical file is
    compared regardless of trailing slashes or redundant separators in the
    absolute paths the caller hands us.
    """
    tree: dict = {}
    for dirpath, dirnames, filenames in os.walk(root):
        # Sort for deterministic traversal order. This does not change the
        # set of discovered files, but it makes any future ordering-dependent
        # extension predictable.
        dirnames.sort()
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            # Normalize to forward-slash form so a path computed on Windows
            # matches one computed on POSIX when the caller round-trips through
            # our API. os.sep varies by platform; '/' is stable in our keys.
            rel = rel.replace(os.sep, "/")
            try:
                st = os.lstat(full)
            except FileNotFoundError:
                # A file disappeared between os.walk and lstat. Skip it; it
                # cannot be synced anyway, and the next sync will reconcile.
                continue
            tree[rel] = st
    return tree


def _same_contents(a: str, b: str, chunk: int = 65536) -> bool:
    """Byte-compare two files, short-circuiting on size then content.

    We compare size first via os.stat to avoid touching file contents at all
    when the sizes differ — the common case for a changed file. When sizes
    match we stream both files in equal chunks to keep peak memory flat even
    for very large files, and we exit on the first differing byte.
    """
    sa = os.stat(a)
    sb = os.stat(b)
    if sa.st_size != sb.st_size:
        return False
    with open(a, "rb") as fa, open(b, "rb") as fb:
        while True:
            ca = fa.read(chunk)
            cb = fb.read(chunk)
            if ca != cb:
                return False
            if not ca:
                return True


def _copy_one(src: str, dst: str) -> None:
    """Copy *src* to *dst*, recreating mode.

    shutil.copy2 copies content and most metadata, but we found that on some
    platforms the destination's executable bit can lag when the source is on a
    different filesystem. We therefore explicitly re-apply the source's mode
    bits after the copy so a +x on the source is honoured on the destination.
    """
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    shutil.copy2(src, dst)
    st = os.lstat(src)
    try:
        os.chmod(dst, stat.S_IMODE(st.st_mode))
    except OSError:
        # Some filesystems reject chmod on certain mode bits. The content is
        # already in place, so we treat a failed chmod as non-fatal.
        pass


@dataclass
class SyncResult:
    """Outcome of a single :meth:`MirrorSync.run` call.

    The fields are deliberately exhaustive: a caller should be able to decide
    whether a follow-up action is needed (e.g. restarting a service) from
    these counts alone, without re-walking the tree.
    """

    copied: List[str] = field(default_factory=list)
    updated: List[str] = field(default_factory=list)
    deleted: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when no errors were recorded."""
        return not self.errors


@dataclass
class MirrorSync:
    """Mirror a source directory tree onto a destination directory.

    The sync is destructive on the *destination*: files absent from the source
    are removed from the destination so that the two trees end with identical
    relative contents. The source is never modified.

    Parameters
    ----------
    source:
        Directory to mirror *from*. Must exist.
    destination:
        Directory to mirror *into*. Created if missing.
    clock:
        Optional callable returning the current time as a float, used only so
        tests can inject a deterministic clock. It is not consulted for any
        correctness-sensitive decision; we include it so tests that want to
        record "when" a sync ran can do so without monkeypatching. Keeping it
        explicit avoids hidden global-state coupling.
    """

    source: str
    destination: str
    clock: Callable[[], float] = field(default=time.time)

    def __post_init__(self) -> None:
        if not os.path.isdir(self.source):
            raise NotADirectoryError(f"source is not a directory: {self.source}")
        if os.path.exists(self.destination) and not os.path.isdir(self.destination):
            raise NotADirectoryError(
                f"destination exists and is not a directory: {self.destination}"
            )
        os.makedirs(self.destination, exist_ok=True)
        # Normalize so downstream path math does not depend on caller slashes.
        self.source = os.path.abspath(self.source)
        self.destination = os.path.abspath(self.destination)

    def run(self) -> SyncResult:
        """Perform one mirror pass and return what changed."""
        result = SyncResult()
        src_tree = _relative_tree(self.source)
        dst_tree = _relative_tree(self.destination)

        # 1. Copy new/updated files from source.
        for rel, s_st in src_tree.items():
            src_full = os.path.join(self.source, rel.replace("/", os.sep))
            dst_full = os.path.join(self.destination, rel.replace("/", os.sep))
            try:
                d_st = dst_tree.get(rel)
                if d_st is None:
                    _copy_one(src_full, dst_full)
                    result.copied.append(rel)
                elif not _same_contents(src_full, dst_full):
                    _copy_one(src_full, dst_full)
                    result.updated.append(rel)
                else:
                    # Content matches. Re-apply mode so a permission-only
                    # change on the source still propagates.
                    if stat.S_IMODE(d_st.st_mode) != stat.S_IMODE(s_st.st_mode):
                        try:
                            os.chmod(dst_full, stat.S_IMODE(s_st.st_mode))
                            result.updated.append(rel)
                        except OSError as exc:
                            result.errors.append(f"{rel}: {exc}")
            except OSError as exc:
                result.errors.append(f"{rel}: {exc}")

        # 2. Delete files in destination that are absent from source.
        for rel in dst_tree:
            if rel not in src_tree:
                dst_full = os.path.join(self.destination, rel.replace("/", os.sep))
                try:
                    os.remove(dst_full)
                    result.deleted.append(rel)
                except OSError as exc:
                    result.errors.append(f"{rel}: {exc}")

        # 3. Prune empty directories in destination to keep the mirror exact.
        self._prune_empty_dirs()

        result.copied.sort()
        result.updated.sort()
        result.deleted.sort()
        result.errors.sort()
        return result

    def _prune_empty_dirs(self) -> None:
        """Remove destination subdirectories that have become empty.

        We walk bottom-up so that deleting a file in a leaf directory can make
        its parent eligible for deletion in the same pass. We never delete the
        destination root itself.
        """
        for dirpath, dirnames, filenames in os.walk(self.destination, topdown=False):
            if os.path.abspath(dirpath) == self.destination:
                continue
            if not dirnames and not filenames:
                try:
                    os.rmdir(dirpath)
                except OSError:
                    # Not empty after all, or vanished — either way, leave it.
                    pass
