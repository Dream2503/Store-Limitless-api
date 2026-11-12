"""Regression tests for the new StoreLimitless SDK model wrappers.

These tests use the standard library's unittest and mock support so maintainers
can run them without a live StoreLimitless instance or account credentials.
Run with: python -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timezone
from ftplib import error_perm
from unittest.mock import MagicMock, PropertyMock, patch
from storelimitless import Directory, File, FileTree, FTPClient, StoreLimitless, StoreLimitlessFTPError, TorrentPreview, UploadJob, UploadTask, User


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
             patch.object(self.file, "private_download_link", return_value=("https://example.invalid/public/download/token", 3600)), \
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


class AuthenticationTransportTests(unittest.TestCase):
    """Verify authentication payloads and initialization/fallback behavior."""

    def test_login_sends_credentials_as_json(self) -> None:
        response = SimpleNamespace(json=lambda: [
            {"access_token": "test-token", "token_type": "bearer"},
            {"username": "demo", "first_name": "Test", "last_name": "User"},
            {"id": 1, "path": "/home"},
        ])
        fake_user = object()
        with patch.object(StoreLimitless, "_is_server_running", return_value=True), \
             patch.object(StoreLimitless, "request", return_value=response) as request, \
             patch("storelimitless.user.User", return_value=fake_user):
            result = StoreLimitless.login("demo", "secret")

        self.assertIs(result, fake_user)
        args, kwargs = request.call_args
        self.assertEqual(args[1:3], ("POST", "/auth/login"))
        self.assertEqual(kwargs["json"], {"username": "demo", "password": "secret"})
        self.assertNotIn("params", kwargs)

    def test_register_sends_profile_fields_as_json(self) -> None:
        with patch.object(StoreLimitless, "_is_server_running", return_value=True), \
             patch.object(StoreLimitless, "request") as request:
            StoreLimitless.register("Ada", "Lovelace", "ada", "secret")
        args, kwargs = request.call_args
        self.assertEqual(args[1:3], ("POST", "/auth/register"))
        self.assertEqual(kwargs["json"], {
            "first_name": "Ada",
            "last_name": "Lovelace",
            "username": "ada",
            "password": "secret",
        })
        self.assertNotIn("params", kwargs)

    def test_initialize_uses_ready_remote_without_starting_binary(self) -> None:
        with patch.object(StoreLimitless, "_is_server_running", return_value=True), \
             patch("storelimitless.auth.Popen") as process:
            StoreLimitless.initialize(server_ip="https://cdn-storelimitless.vercel.app")
        self.assertEqual(StoreLimitless.API_URL, "https://cdn-storelimitless.vercel.app")
        process.assert_not_called()

    def test_initialize_falls_back_to_local_when_remote_is_unavailable(self) -> None:
        with patch.object(StoreLimitless, "_is_server_running", side_effect=[False, True]), \
             patch("storelimitless.auth.Popen") as process:
            StoreLimitless.initialize(server_ip="https://unavailable.example")
        self.assertEqual(StoreLimitless.API_URL, "http://127.0.0.1:8000")
        process.assert_not_called()

    def test_initialize_can_require_remote_without_fallback(self) -> None:
        from storelimitless import StoreLimitlessConnectionError

        with patch.object(StoreLimitless, "_is_server_running", return_value=False):
            with self.assertRaises(StoreLimitlessConnectionError):
                StoreLimitless.initialize(
                    server_ip="https://unavailable.example",
                    fallback_to_local=False,
                )

    def test_readiness_requires_ready_json_true(self) -> None:
        good = SimpleNamespace(ok=True, json=lambda: {"ready": True})
        bad = SimpleNamespace(ok=True, json=lambda: {"ready": False})
        with patch("storelimitless.auth.get", return_value=good):
            self.assertTrue(StoreLimitless._is_server_running())
        with patch("storelimitless.auth.get", return_value=bad):
            self.assertFalse(StoreLimitless._is_server_running())


class DirectoryImportTests(unittest.TestCase):
    """Cover remote link-import request construction and synchronous URL upload."""

    def setUp(self) -> None:
        self.user = SimpleNamespace(username="demo", token="token", home=None)
        self.directory = Directory(self.user, 1, Path("/home"), None, None)  # type: ignore[arg-type]
        self.user.home = self.directory

    def test_local_upload_sends_chunks_and_polls_public_status(self) -> None:
        stored_file = SimpleNamespace(id=42, name="chunked.bin")
        requests = []

        def fake_request(user, method, path, **kwargs):
            requests.append((method, path, kwargs))
            if path == "/auth/upload":
                return SimpleNamespace(json=lambda: {"job_id": "job-chunks"})
            if path == "/public/upload/job-chunks/status":
                return SimpleNamespace(json=lambda: {
                    "status": "completed",
                    "message": "Upload complete of id=42",
                    "file_id": 42,
                    "transfer": 7,
                    "total": 7,
                })
            return SimpleNamespace(ok=True)

        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "chunked.bin"
            source.write_bytes(b"abcdefg")
            with patch.object(Directory, "ls", new_callable=PropertyMock, return_value=SimpleNamespace(files=[stored_file])), \
                 patch("storelimitless.directory.StoreLimitless.request", side_effect=fake_request), \
                 patch("storelimitless.directory.tqdm"):
                result = self.directory.upload(
                    source,
                    data_center="Telegram",
                    chunk_size=3,
                    poll_interval=0,
                    quiet=True,
                )

        self.assertIs(result, stored_file)
        self.assertEqual(requests[0][0:2], ("POST", "/auth/upload"))
        self.assertEqual(requests[0][2]["headers"]["X-Data-Center"], "Telegram")
        chunk_calls = [entry for entry in requests if entry[1] == "/public/upload/job-chunks/chunk"]
        self.assertEqual(
            [(call[2]["params"]["offset"], call[2]["data"]) for call in chunk_calls],
            [(0, b"abc"), (3, b"def"), (6, b"g")],
        )
        self.assertEqual(requests[-1][0:2], ("GET", "/public/upload/job-chunks/status"))

    def test_import_link_submits_magnet_indices_and_batch(self) -> None:
        response = SimpleNamespace(json=lambda: {"job_id": "job-42"})
        with patch("storelimitless.auth.StoreLimitless.request", return_value=response) as request:
            job = self.directory.import_link(
                "magnet:?xt=urn:btih:0123456789abcdef",
                selected_files=[0, 4],
                batch_id="batch-3",
            )
        self.assertEqual(job.job_id, "job-42")
        self.assertEqual(job.status, "uploading")
        self.assertEqual(request.call_args.kwargs["params"]["selected_files"], "[0, 4]")
        self.assertEqual(request.call_args.kwargs["params"]["batch_id"], "batch-3")

    def test_import_link_rejects_duplicate_torrent_indices(self) -> None:
        with patch("storelimitless.auth.StoreLimitless.request") as request:
            with self.assertRaises(ValueError):
                self.directory.import_link(
                    "magnet:?xt=urn:btih:hash",
                    selected_files=[1, 1],
                )
            request.assert_not_called()

    def test_import_link_rejects_torrent_indices_for_http_urls(self) -> None:
        with patch("storelimitless.auth.StoreLimitless.request") as request:
            with self.assertRaises(ValueError):
                self.directory.import_link(
                    "https://example.com/file.zip",
                    selected_files=[0],
                )
            request.assert_not_called()

    def test_upload_waits_for_http_import_and_returns_new_file(self) -> None:
        old_file = SimpleNamespace(id=10, name="old.txt")
        imported_file = SimpleNamespace(id=11, name="report.txt")
        listings = [
            SimpleNamespace(files=[old_file]),
            SimpleNamespace(files=[old_file, imported_file]),
        ]
        job = SimpleNamespace(
            job_id="job-http",
            name="report.txt",
            status="uploading",
            message="Import queued",
            refresh=lambda: setattr(job, "status", "completed") or job,
        )
        with patch.object(Directory, "ls", new_callable=PropertyMock, side_effect=listings), \
             patch.object(self.directory, "import_link", return_value=job) as import_link:
            result = self.directory.upload(
                "https://example.com/report.txt",
                quiet=True,
                poll_interval=0,
                job_timeout=1,
            )
        self.assertIs(result, imported_file)
        import_link.assert_called_once()


class FileSecurityAndSharingTests(unittest.TestCase):
    """Ensure private file operations do not mint public links."""

    def setUp(self) -> None:
        user = SimpleNamespace(username="demo", token="test")
        directory = SimpleNamespace(user=user, path=Path("/home"))
        self.file = File(
            directory=directory,  # type: ignore[arg-type]
            id=7,
            name="notes.txt",
            type="text/plain",
            size=5,
            modified_at=datetime.now(timezone.utc),
            deleted_at=None,
            data_center="Discord",
        )

    def test_public_share_link_uses_opaque_share_id(self) -> None:
        response = SimpleNamespace(json=lambda: {
            "share_id": "opaque-share-id",
            "public_token": "legacy-token",
        })
        with patch("storelimitless.file.StoreLimitless.request", return_value=response) as request:
            url = self.file.public_share_link()
        self.assertTrue(url.endswith("/public/stream/opaque-share-id"))
        request.assert_called_once_with(self.file.user, "POST", "/auth/file/7/public-link")

    def test_public_metadata_uses_opaque_share_id(self) -> None:
        response = SimpleNamespace(json=lambda: {
            "id": 7,
            "name": "notes.txt",
            "type": "text/plain",
            "size": 5,
        })
        with patch.object(self.file, "public_share_id", return_value="opaque-share-id"), \
             patch("storelimitless.auth.StoreLimitless.request", return_value=response) as request:
            metadata = self.file.public_metadata()
        self.assertEqual(metadata["name"], "notes.txt")
        request.assert_called_once_with(None, "GET", "/public/metadata/opaque-share-id")

    def test_download_uses_session_bound_url(self) -> None:
        class FakeResponse:
            headers = {"Content-Length": "5"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def iter_content(self, chunk_size):
                yield b"hello"

        response = FakeResponse()
        with patch.object(self.file, "private_download_link", return_value=("https://example.invalid/public/download/private", 60)), \
             patch.object(StoreLimitless, "API_URL", "https://example.invalid"), \
             patch("storelimitless.file.StoreLimitless.request", return_value=response) as request, \
             patch("storelimitless.file.tqdm") as progress:
            with __import__("tempfile").TemporaryDirectory() as folder:
                path = self.file.download(Path(folder) / "notes.txt", quiet=True)
                self.assertEqual(path.read_bytes(), b"hello")
        self.assertEqual(request.call_args.args[2], "/public/download/private")
        progress.assert_called_once()


class FTPClientTests(unittest.TestCase):
    """Exercise the optional FTP client without opening a network connection."""

    def setUp(self) -> None:
        self.client = object.__new__(FTPClient)
        self.client.host = "ftp.example.test"
        self.client.port = 2121
        self.client.username = "demo"
        self.client.passive = True
        self.client._ftp = MagicMock()
        self.client._closed = False

    def test_connect_authenticates_and_sets_passive_mode(self) -> None:
        with patch("storelimitless.ftp.FTP") as ftp_type:
            ftp = ftp_type.return_value
            client = FTPClient("ftp.example.test", "demo", "secret", port=2121)
        ftp.connect.assert_called_once_with("ftp.example.test", 2121, timeout=30.0)
        ftp.login.assert_called_once_with("demo", "secret")
        ftp.set_pasv.assert_called_once_with(True)
        self.assertEqual(client.host, "ftp.example.test")
        client.close()

    def test_invalid_remote_path_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.client.cwd("/home\nQUIT")

    def test_listdir_and_navigation_use_ftp_commands(self) -> None:
        self.client._ftp.pwd.return_value = "/"
        self.client._ftp.nlst.return_value = ["notes.txt", "photos"]
        self.assertEqual(self.client.listdir(), ["notes.txt", "photos"])
        self.assertEqual(self.client.cwd("/photos"), "/")
        self.client._ftp.cwd.assert_called_once_with("/photos")

    def test_listdir_details_uses_mlsd_and_normalizes_facts(self) -> None:
        self.client._ftp.mlsd.return_value = iter([
            ("notes.txt", {"type": "file", "size": "5"}),
            ("photos", {"type": "dir"}),
        ])
        rows = self.client.listdir_details("/")
        self.assertEqual(rows[0], ("notes.txt", {"type": "file", "size": "5"}))
        self.client._ftp.mlsd.assert_called_once_with("/")

    def test_append_bytes_uses_ftp_appe(self) -> None:
        captured = {}

        def transfer(command, stream, blocksize=8192):
            captured["command"] = command
            captured["content"] = stream.read()
            return "226 Transfer complete."

        self.client._ftp.storbinary.side_effect = transfer
        self.client.append_bytes(b"tail", "/new-file.txt")
        self.assertEqual(captured["command"], "APPE /new-file.txt")
        self.assertEqual(captured["content"], b"tail")

    def test_delete_and_rmdir_call_soft_delete_ftp_commands(self) -> None:
        self.client._ftp.delete.return_value = "250 File moved to trash."
        self.client._ftp.rmd.return_value = "250 Directory moved to trash."
        self.client.delete("/old.txt")
        self.client.rmdir("/old-folder")
        self.client._ftp.delete.assert_called_once_with("/old.txt")
        self.client._ftp.rmd.assert_called_once_with("/old-folder")

    def test_download_bytes_collects_streamed_file(self) -> None:
        def transfer(_command, callback, blocksize=8192):
            callback(b"hello")
            callback(b" world")
            return "226 Transfer complete."

        self.client._ftp.retrbinary.side_effect = transfer
        self.assertEqual(self.client.download_bytes("/notes.txt"), b"hello world")

    def test_upload_bytes_streams_binary_payload(self) -> None:
        captured = {}

        def transfer(command, stream, blocksize=8192):
            captured["command"] = command
            captured["content"] = stream.read()
            captured["blocksize"] = blocksize
            return "226 Transfer complete."

        self.client._ftp.storbinary.side_effect = transfer
        result = self.client.upload_bytes(b"hello", "/notes.txt")
        self.assertEqual(result, "226 Transfer complete.")
        self.assertEqual(captured["command"], "STOR /notes.txt")
        self.assertEqual(captured["content"], b"hello")

    def test_download_writes_atomically_to_destination(self) -> None:
        def transfer(_command, callback, blocksize=8192):
            callback(b"downloaded")
            return "226 Transfer complete."

        self.client._ftp.retrbinary.side_effect = transfer
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "notes.txt"
            result = self.client.download("/notes.txt", target)
            self.assertEqual(result, target)
            self.assertEqual(target.read_bytes(), b"downloaded")
            self.assertEqual(list(Path(folder).iterdir()), [target])

    def test_upload_streams_local_file(self) -> None:
        captured = {}

        def transfer(command, stream, blocksize=8192):
            captured["command"] = command
            captured["content"] = stream.read()
            return "226 Transfer complete."

        self.client._ftp.storbinary.side_effect = transfer
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "local.txt"
            source.write_bytes(b"local payload")
            self.client.upload(source, "/remote.txt")
        self.assertEqual(captured["command"], "STOR /remote.txt")
        self.assertEqual(captured["content"], b"local payload")

    def test_ftp_protocol_errors_use_sdk_exception(self) -> None:
        self.client._ftp.cwd.side_effect = error_perm("550 Permission denied")
        with self.assertRaises(StoreLimitlessFTPError):
            self.client.cwd("/restricted")

    def test_connection_errors_use_sdk_connection_exception(self) -> None:
        from storelimitless import StoreLimitlessConnectionError

        self.client._ftp.pwd.side_effect = OSError("socket closed")
        with self.assertRaises(StoreLimitlessConnectionError):
            self.client.pwd()


class UserFTPConnectionTests(unittest.TestCase):
    """Ensure authenticated User objects pass their account credentials to FTP."""

    def test_connect_ftp_uses_same_account_credentials(self) -> None:
        user = object.__new__(User)
        user.username = "demo"
        user.password = "secret"
        with patch("storelimitless.ftp.FTPClient.from_user", return_value="ftp-client") as connect:
            result = user.connect_ftp("10.0.0.5", port=2121, timeout=10, passive=False)
        self.assertEqual(result, "ftp-client")
        connect.assert_called_once_with(
            user, "10.0.0.5", port=2121, timeout=10, passive=False
        )


class UserTaskManagementTests(unittest.TestCase):
    """Verify account-level task tray operations."""

    def setUp(self) -> None:
        self.user = object.__new__(User)
        self.user.token = "token"
        self.user.username = "demo"
        self.user.password = "secret"
        self.user.first_name = "Test"
        self.user.last_name = "User"
        self.user.home = Directory(self.user, 1, Path("/home"), None, None)

    def test_clear_upload_tasks_calls_clear_all_endpoint(self) -> None:
        response = SimpleNamespace(json=lambda: {"message": "Upload tray cleared"})
        with patch("storelimitless.auth.StoreLimitless.request", return_value=response) as request:
            result = self.user.clear_upload_tasks()
        self.assertEqual(result["message"], "Upload tray cleared")
        request.assert_called_once_with(self.user, "DELETE", "/auth/upload-batches")

    def test_clear_upload_batches_is_an_alias(self) -> None:
        response = SimpleNamespace(json=lambda: {"message": "Upload tray cleared"})
        with patch("storelimitless.auth.StoreLimitless.request", return_value=response) as request:
            result = self.user.clear_upload_batches()
        self.assertEqual(result["message"], "Upload tray cleared")
        request.assert_called_once_with(self.user, "DELETE", "/auth/upload-batches")


class TaskLifecycleTests(unittest.TestCase):
    """Exercise task/job lifecycle request paths."""

    def setUp(self) -> None:
        self.user = SimpleNamespace(
            username="demo",
            token="test",
            home=SimpleNamespace(path=Path("/home"), id=1),
        )

    def test_upload_task_update_and_cancel(self) -> None:
        task = UploadTask(self.user, "batch-1", {"name": "Documents"})
        update_response = SimpleNamespace(json=lambda: {"batch_id": "batch-1", "status": "uploading"})
        cancel_response = SimpleNamespace(json=lambda: {"batch_id": "batch-1", "status": "cancelled"})
        with patch("storelimitless.auth.StoreLimitless.request", side_effect=[update_response, cancel_response]) as request:
            task.update(message="Starting")
            task.cancel()
        self.assertEqual(task.status, "cancelled")
        self.assertEqual(request.call_args_list[0].args[1:3], ("PATCH", "/auth/upload-batches/batch-1"))
        self.assertEqual(request.call_args_list[1].args[1:3], ("POST", "/auth/upload-batches/batch-1/cancel"))

    def test_upload_job_refresh_cancel_and_failure_report(self) -> None:
        job = UploadJob(self.user, "job-1")
        replies = [
            SimpleNamespace(json=lambda: {"job_id": "job-1", "status": "uploading"}),
            SimpleNamespace(json=lambda: {"job_id": "job-1", "status": "cancelled"}),
            SimpleNamespace(json=lambda: {"job_id": "job-1", "status": "error"}),
        ]
        with patch("storelimitless.auth.StoreLimitless.request", side_effect=replies) as request:
            job.refresh()
            job.cancel()
            job.report_failure("chunk write failed")
        self.assertEqual(job.status, "error")
        self.assertEqual(request.call_args_list[0].args[1:3], ("GET", "/auth/upload-jobs/job-1/status"))
        self.assertEqual(request.call_args_list[1].args[1:3], ("POST", "/auth/upload-jobs/job-1/cancel"))
        self.assertEqual(request.call_args_list[2].kwargs["json"], {"message": "chunk write failed"})



if __name__ == "__main__":
    unittest.main()
