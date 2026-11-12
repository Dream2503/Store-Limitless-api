"""Opt-in live smoke test for a dedicated StoreLimitless test account.

This test mutates only a uniquely named temporary folder in the configured
throwaway account. It is skipped unless all three opt-in environment variables
are present; never point it at an account containing production user data.
Run with:
  STORELIMITLESS_E2E_API_URL=https://cdn-storelimitless.vercel.app
  STORELIMITLESS_E2E_USERNAME=...
  STORELIMITLESS_E2E_PASSWORD=...
  STORELIMITLESS_E2E_ALLOW_MUTATIONS=1
  python -m unittest tests.live_smoke -v
"""

from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from pathlib import Path
from zipfile import is_zipfile

from storelimitless import StoreLimitless


API_URL = os.getenv("STORELIMITLESS_E2E_API_URL", "https://cdn-storelimitless.vercel.app")
USERNAME = os.getenv("STORELIMITLESS_E2E_USERNAME", "")
PASSWORD = os.getenv("STORELIMITLESS_E2E_PASSWORD", "")
ALLOW_MUTATIONS = os.getenv("STORELIMITLESS_E2E_ALLOW_MUTATIONS") == "1"
ENABLED = bool(USERNAME and PASSWORD and ALLOW_MUTATIONS)


@unittest.skipUnless(
    ENABLED,
    "Live tests require a dedicated test account and STORELIMITLESS_E2E_ALLOW_MUTATIONS=1",
)
class LiveBackendSmokeTests(unittest.TestCase):
    """Run a complete, isolated folder/file workflow against a live server."""

    @classmethod
    def setUpClass(cls) -> None:
        StoreLimitless.initialize(server_ip=API_URL, fallback_to_local=False)
        cls.user = StoreLimitless.login(USERNAME, PASSWORD)
        cls.folder_name = f"sdk-e2e-{uuid.uuid4().hex[:12]}"
        cls.root = cls.user.home.mkdir(cls.folder_name)
        cls.temporary = tempfile.TemporaryDirectory(prefix="storelimitless-e2e-")
        cls.local_dir = Path(cls.temporary.name)

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            root = getattr(cls, "root", None)
            if root is not None:
                try:
                    root.rm()
                except Exception:
                    pass
                try:
                    root.delete()
                except Exception:
                    pass
            user = getattr(cls, "user", None)
            if user is not None and user.token:
                user.logout()
        finally:
            temporary = getattr(cls, "temporary", None)
            if temporary is not None:
                temporary.cleanup()
            StoreLimitless.close()

    def test_account_filesystem_upload_links_and_lifecycle(self) -> None:
        nested = self.root.mkdir("nested")
        source = self.local_dir / "smoke.txt"
        source.write_text("StoreLimitless SDK live smoke test\n", encoding="utf-8")

        uploaded = nested.upload(source, data_center="Discord", quiet=True)
        original_id = uploaded.id
        self.assertEqual(uploaded.read_text(), "StoreLimitless SDK live smoke test\n")

        uploaded.save_text("Updated by the StoreLimitless SDK\n")
        self.assertEqual(uploaded.id, original_id)
        self.assertEqual(uploaded.read_text(), "Updated by the StoreLimitless SDK\n")

        destination = self.root.mkdir("destination")
        uploaded.rename("renamed.txt")
        uploaded.move(destination)
        self.assertEqual(uploaded.directory.path, destination.path)

        second_source = self.local_dir / "second.txt"
        second_source.write_text("second file\n", encoding="utf-8")
        second = destination.upload(second_source, data_center="Discord", quiet=True)
        bulk_destination = self.root.mkdir("bulk")
        result = self.user.move([uploaded, second], bulk_destination)
        self.assertFalse(result.get("failed", []), result)
        self.assertEqual(len(result.get("moved", [])), 2)

        matches = self.user.search("renamed.txt")
        self.assertTrue(any(item.id == uploaded.id for item in matches.files))

        tree = self.user.tree
        self.assertTrue(any(item.id == uploaded.id for item in tree.active_files))
        self.assertGreaterEqual(tree.storage_used_bytes, uploaded.size)

        private_url, private_expiry = uploaded.private_download_link()
        self.assertGreater(private_expiry, 0)
        private_head = StoreLimitless.request(
            self.user, "HEAD", private_url.removeprefix(StoreLimitless.API_URL), timeout=30
        )
        self.assertTrue(private_head.ok)

        share_url = uploaded.public_share_link(download=True)
        public_head = StoreLimitless.request(
            None, "HEAD", share_url.removeprefix(StoreLimitless.API_URL), timeout=30
        )
        self.assertTrue(public_head.ok)

        archive = self.local_dir / "sdk-smoke.zip"
        self.user.download_zip([self.root], path=archive, quiet=True)
        self.assertTrue(is_zipfile(archive))

        uploaded.rm()
        self.assertTrue(any(item.id == uploaded.id for item in self.user.trash.files))
        uploaded.restore()
        self.assertIsNone(uploaded.deleted_at)

        task = self.user.create_upload_task(
            "SDK live smoke task",
            [{"name": "metadata-only.txt", "size": 1}],
            directory=self.root,
            data_center="Discord",
            kind="file",
        )
        task.update(message="SDK live smoke update")
        task.cancel()
        task.refresh()
        self.assertEqual(task.status, "cancelled")
        task.dismiss()

        self.root.rename(f"{self.folder_name}-renamed")
        self.assertEqual(self.root.path.name, f"{self.folder_name}-renamed")
        self.root.rm()
        self.assertTrue(any(item.id == self.root.id for item in self.user.trash.directories))
        self.root.restore()
        self.assertIsNone(self.root.deleted_at)


if __name__ == "__main__":
    unittest.main()
