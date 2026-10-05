import os
import stat
import tempfile
import unittest

from directory_mirror_sync import MirrorSync, SyncResult


class _BaseCase(unittest.TestCase):
    def setUp(self) -> None:
        self.src = tempfile.TemporaryDirectory()
        self.dst = tempfile.TemporaryDirectory()
        self.addCleanup(self.src.cleanup)
        self.addCleanup(self.dst.cleanup)
        self.source = self.src.name
        self.destination = self.dst.name

    def _write(self, root: str, rel: str, data: bytes, mode: int = 0o644) -> None:
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        os.chmod(path, mode)

    def _read(self, root: str, rel: str) -> bytes:
        with open(os.path.join(root, rel), "rb") as fh:
            return fh.read()


class TestCopiesNewFiles(_BaseCase):
    def test_single_new_file(self) -> None:
        self._write(self.source, "a.txt", b"hello")
        result = MirrorSync(self.source, self.destination).run()
        self.assertEqual(result.copied, ["a.txt"])
        self.assertEqual(result.updated, [])
        self.assertEqual(result.deleted, [])
        self.assertEqual(self._read(self.destination, "a.txt"), b"hello")

    def test_nested_new_files(self) -> None:
        self._write(self.source, "a/b/c.txt", b"deep")
        self._write(self.source, "top.txt", b"flat")
        result = MirrorSync(self.source, self.destination).run()
        self.assertEqual(sorted(result.copied), ["a/b/c.txt", "top.txt"])
        self.assertEqual(self._read(self.destination, "a/b/c.txt"), b"deep")


class TestUpdatesChanged(_BaseCase):
    def test_content_change_is_updated(self) -> None:
        self._write(self.source, "f.txt", b"v1")
        self._write(self.destination, "f.txt", b"v2")
        result = MirrorSync(self.source, self.destination).run()
        self.assertEqual(result.updated, ["f.txt"])
        self.assertEqual(self._read(self.destination, "f.txt"), b"v1")

    def test_same_content_not_listed(self) -> None:
        self._write(self.source, "f.txt", b"same")
        self._write(self.destination, "f.txt", b"same")
        result = MirrorSync(self.source, self.destination).run()
        self.assertEqual(result.copied, [])
        self.assertEqual(result.updated, [])
        self.assertEqual(result.deleted, [])

    def test_size_only_differ_short_circuits(self) -> None:
        self._write(self.source, "f.txt", b"short")
        self._write(self.destination, "f.txt", b"much longer content")
        result = MirrorSync(self.source, self.destination).run()
        self.assertEqual(result.updated, ["f.txt"])
        self.assertEqual(self._read(self.destination, "f.txt"), b"short")


class TestModePropagation(_BaseCase):
    def test_mode_only_change_is_updated(self) -> None:
        self._write(self.source, "bin", b"x", mode=0o755)
        self._write(self.destination, "bin", b"x", mode=0o644)
        result = MirrorSync(self.source, self.destination).run()
        self.assertEqual(result.updated, ["bin"])
        mode = stat.S_IMODE(os.stat(os.path.join(self.destination, "bin")).st_mode)
        self.assertEqual(mode, 0o755)


class TestDeletion(_BaseCase):
    def test_removed_file_is_deleted(self) -> None:
        self._write(self.source, "keep.txt", b"k")
        self._write(self.destination, "keep.txt", b"k")
        self._write(self.destination, "gone.txt", b"g")
        result = MirrorSync(self.source, self.destination).run()
        self.assertEqual(result.deleted, ["gone.txt"])
        self.assertFalse(os.path.exists(os.path.join(self.destination, "gone.txt")))

    def test_empty_dirs_are_pruned(self) -> None:
        self._write(self.destination, "sub/gone.txt", b"g")
        result = MirrorSync(self.source, self.destination).run()
        self.assertEqual(result.deleted, ["sub/gone.txt"])
        self.assertFalse(os.path.exists(os.path.join(self.destination, "sub")))

    def test_destination_root_is_not_pruned(self) -> None:
        result = MirrorSync(self.source, self.destination).run()
        self.assertTrue(os.path.isdir(self.destination))
        self.assertEqual(result.copied, [])
        self.assertEqual(result.deleted, [])


class TestConstructor(_BaseCase):
    def test_source_must_exist(self) -> None:
        with self.assertRaises(NotADirectoryError):
            MirrorSync("/does/not/exist", self.destination)

    def test_destination_must_be_dir_if_present(self) -> None:
        f = os.path.join(self.source, "file")
        self._write(self.source, "file", b"x")
        with self.assertRaises(NotADirectoryError):
            MirrorSync(self.source, f)

    def test_destination_created_if_missing(self) -> None:
        new_dst = os.path.join(self.destination, "new", "deep")
        self._write(self.source, "a.txt", b"hi")
        result = MirrorSync(self.source, new_dst).run()
        self.assertEqual(result.copied, ["a.txt"])
        self.assertEqual(self._read(new_dst, "a.txt"), b"hi")


class TestResultShape(_BaseCase):
    def test_result_is_syncresult(self) -> None:
        self._write(self.source, "a.txt", b"x")
        result = MirrorSync(self.source, self.destination).run()
        self.assertIsInstance(result, SyncResult)
        self.assertTrue(result.ok)


class TestIdempotent(_BaseCase):
    def test_second_run_is_noop(self) -> None:
        self._write(self.source, "a/b.txt", b"data")
        sync = MirrorSync(self.source, self.destination)
        first = sync.run()
        self.assertEqual(first.copied, ["a/b.txt"])
        second = sync.run()
        self.assertEqual(second.copied, [])
        self.assertEqual(second.updated, [])
        self.assertEqual(second.deleted, [])


class TestLargeFile(_BaseCase):
    def test_large_differing_file(self) -> None:
        # Use a content pattern that would catch a chunk-boundary bug:
        # identical head, differing tail.
        head = b"A" * (65536 + 100)
        self._write(self.source, "big.bin", head + b"X")
        self._write(self.destination, "big.bin", head + b"Y")
        result = MirrorSync(self.source, self.destination).run()
        self.assertEqual(result.updated, ["big.bin"])
        self.assertEqual(self._read(self.destination, "big.bin"), head + b"X")


class TestErrorHandling(_BaseCase):
    def test_unreadable_source_file_records_error(self) -> None:
        self._write(self.source, "secret.txt", b"hidden", mode=0o000)
        try:
            result = MirrorSync(self.source, self.destination).run()
            # On some platforms root can still read 0o000 files; tolerate both.
            if result.errors:
                self.assertFalse(result.ok)
            else:
                self.assertEqual(result.copied, ["secret.txt"])
        finally:
            os.chmod(os.path.join(self.source, "secret.txt"), 0o644)


if __name__ == "__main__":
    unittest.main()
