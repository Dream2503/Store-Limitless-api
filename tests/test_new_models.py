"""Regression tests for the new StoreLimitless SDK model wrappers.

These tests use the standard library's unittest and mock support so maintainers
can run them without a live StoreLimitless instance or account credentials.
Run with: python -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timezone
from unittest.mock import patch
from storelimitless import File, FileTree, TorrentPreview, UploadJob, UploadTask


class TaskModelTests(unittest.TestCase):
    """Exercise normalization and lifecycle requests on task/job models."""

    def setUp(self) -> None:
        """Provide a minimal fake user with the attributes wrappers require."""
        self.user = SimpleNamespace(
            username="demo",
            token="unit-test-token",
            home=SimpleNamespace(path=Path("/home"), id=1),
        )

    def test_upload_task_maps_progress_fields(self) -> None:
        """Task snapshots should expose item counts and retain unknown metadata."""
        task = UploadTask(
            self.user,  # type: ignore[arg-type]
            "batch-1",
            {
                "batch_id": "batch-1",
                "name": "Documents",
                "status": "uploading",
                "items": [
                    {"name": "a.pdf", "status": "completed"},
                    {"name": "b.pdf", "status": "uploading"},
                ],
                "completed_items": 1,
                "total_items": 2,
                "future_field": "preserved",
            },
        )

        self.assertEqual(task.name, "Documents")
        self.assertEqual(task.status, "uploading")
        self.assertEqual(task.completed_items, 1)
        self.assertEqual(task.total_items, 2)
        self.assertEqual(task.data["future_field"], "preserved")

    def test_empty_task_id_is_rejected(self) -> None:
        """An empty batch ID must not create an unusable model."""
        with self.assertRaises(ValueError):
            UploadTask(self.user, " ")  # type: ignore[arg-type]

    def test_upload_job_maps_transfer_metrics(self) -> None:
        """Separate download and upload metrics should remain independently visible."""
        job = UploadJob(
            self.user,  # type: ignore[arg-type]
            "job-1",
            {
                "job_id": "job-1",
                "name": "movie.mkv",
                "status": "uploading",
                "transfer": 120,
                "total": 500,
                "download_transfer": 250,
                "download_total": 500,
                "download_active": True,
                "download_speed": 12.5,
                "upload_speed": 9.0,
                "download_eta": 20,
                "upload_eta": None,
            },
        )

        self.assertEqual(job.transfer, 120)
        self.assertEqual(job.total, 500)
        self.assertEqual(job.download_transfer, 250)
        self.assertEqual(job.download_speed, 12.5)
        self.assertEqual(job.upload_speed, 9.0)
        self.assertEqual(job.download_eta, 20.0)
        self.assertIsNone(job.upload_eta)

    def test_torrent_preview_keeps_original_indices(self) -> None:
        """Visible row positions must not replace source torrent indices."""
        preview = TorrentPreview(
            "sample",
            [
                {"index": 0, "path": "movie.mkv", "size": 100},
                {"index": 4, "path": "sample.srt", "size": 10},
                {"index": True, "path": "invalid", "size": 900},
            ],
        )

        self.assertEqual(preview.indices, [0, 4])
        self.assertEqual(preview.total_size, 1010)
        self.assertEqual(len(list(preview)), 3)


class FileTextApiTests(unittest.TestCase):
    """Exercise file text helpers without a live API server."""

    def setUp(self) -> None:
        self.user = SimpleNamespace(username="demo", token="test")
        self.directory = SimpleNamespace(user=self.user, path=Path("/home"))
        self.file = File(
            directory=self.directory,  # type: ignore[arg-type]
            id=7,
            name="notes.txt",
            type="text/plain",
            size=11,
            modified_at=datetime.now(timezone.utc),
            deleted_at=None,
            data_center="Discord",
        )

    def test_read_text_decodes_streamed_bytes(self) -> None:
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def iter_content(self, chunk_size):
                self.assert_chunk_size = chunk_size
                yield b"hello "
                yield b"world"

        fake = FakeResponse()
        with patch.object(self.file, "metadata", return_value={"id": 7, "name": "notes.txt", "type": "text/plain", "size": 11}), \
             patch.object(self.file, "public_link", return_value="https://example.invalid/public/download/token"), \
             patch("storelimitless.file.StoreLimitless.request", return_value=fake):
            self.assertEqual(self.file.read_text(), "hello world")
        self.assertEqual(fake.assert_chunk_size, 64 * 1024)

    def test_read_text_rejects_oversized_metadata_before_download(self) -> None:
        with patch.object(self.file, "metadata", return_value={"id": 7, "name": "notes.txt", "type": "text/plain", "size": 100}), \
             patch("storelimitless.file.StoreLimitless.request") as request:
            with self.assertRaises(ValueError):
                self.file.read_text(max_bytes=10)
            request.assert_not_called()

    def test_save_text_posts_raw_utf8_and_updates_snapshot(self) -> None:
        from unittest.mock import patch

        class FakeResponse:
            def json(self):
                return {
                    "id": 7,
                    "name": "notes.txt",
                    "size": 5,
                    "modified_at": "2026-11-12T10:00:00+00:00",
                }

        with patch("storelimitless.file.StoreLimitless.request", return_value=FakeResponse()) as request:
            result = self.file.save_text("hello")
        self.assertIs(result, self.file)
        self.assertEqual(self.file.size, 5)
        args, kwargs = request.call_args
        self.assertEqual(args[1:3], ("PUT", "/auth/file/7/content"))
        self.assertEqual(kwargs["data"], b"hello")
        self.assertEqual(kwargs["headers"]["Content-Type"], "application/octet-stream")

    def test_save_text_rejects_oversized_content_locally(self) -> None:
        from unittest.mock import patch

        with patch("storelimitless.file.StoreLimitless.request") as request:
            with self.assertRaises(ValueError):
                self.file.save_text("x" * 11, max_bytes=10)
            request.assert_not_called()


class FileTreeTests(unittest.TestCase):
    """Verify the whole-tree conversion preserves parent paths and deletion state."""

    def setUp(self) -> None:
        """Create a minimal authenticated-user stand-in."""
        self.user = SimpleNamespace(
            username="demo",
            token="unit-test-token",
            home=SimpleNamespace(path=Path("/home"), id=1),
        )

    def test_tree_converts_directories_and_files(self) -> None:
        """Tree records should become SDK objects with active/trash partitions."""
        from datetime import datetime, timezone

        tree = FileTree.from_payload(
            self.user,  # type: ignore[arg-type]
            [
                {
                    "id": 1,
                    "path": "/home",
                    "modified_at": datetime.now(timezone.utc).isoformat(),
                    "deleted_at": None,
                },
                {
                    "id": 2,
                    "path": "/home/trash-folder",
                    "modified_at": datetime.now(timezone.utc).isoformat(),
                    "deleted_at": None,
                    "is_deleted": True,
                },
            ],
            [
                {
                    "id": 10,
                    "name": "active.txt",
                    "type": "text/plain",
                    "size": 50,
                    "modified_at": datetime.now(timezone.utc).isoformat(),
                    "deleted_at": None,
                    "data_center": "Discord",
                    "directory": "/home",
                },
                {
                    "id": 11,
                    "name": "deleted.txt",
                    "type": "text/plain",
                    "size": 75,
                    "modified_at": datetime.now(timezone.utc).isoformat(),
                    "deleted_at": None,
                    "is_deleted": True,
                    "data_center": "Discord",
                    "directory": "/home/trash-folder",
                },
            ],
            125,
        )

        self.assertEqual(tree.storage_used_bytes, 125)
        self.assertEqual([item.name for item in tree.active_files], ["active.txt"])
        self.assertEqual([item.name for item in tree.trashed_files], ["deleted.txt"])
        self.assertEqual([item.path for item in tree.trashed_directories], [Path("/home/trash-folder")])
        self.assertEqual(tree.files[0].directory.path, Path("/home"))
        self.assertEqual(tree.files[1].directory.path, Path("/home/trash-folder"))


if __name__ == "__main__":
    unittest.main()
