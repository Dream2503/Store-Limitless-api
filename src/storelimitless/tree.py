"""Typed object model for the StoreLimitless whole-account file-tree endpoint.

The backend /auth/tree endpoint returns all active and trashed objects in one
response. FileTree preserves that snapshot as SDK Directory and File objects,
and exposes active/deleted partitions and the server-computed storage total.
The class does not lazily fetch children because the entire tree is already
returned by the endpoint.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from .exception import StoreLimitlessResponseError
from .utils import directory_from_payload

if TYPE_CHECKING:
    from .directory import Directory
    from .file import File
    from .user import User


class FileTree:
    """Represent the complete account-wide snapshot returned by GET /auth/tree.

    Attributes:
        directories: All directory objects, including active and trashed items.
        files: All file objects, including active and trashed items.
        storage_used_bytes: Backend-calculated total including active and
            trashed files. This value is not recalculated on the client.

    Each Directory or File carries its deletion state through deleted_at.
    The collections are snapshots and will not update automatically when
    objects or server-side records change.
    """

    def __init__(self, directories: list[Directory], files: list[File], storage_used_bytes: int) -> None:
        """Build a tree snapshot from SDK objects and server-reported usage."""
        self.directories: list[Directory] = directories
        self.files: list[File] = files
        self.storage_used_bytes: int = storage_used_bytes

    @classmethod
    def from_payload(cls, user: User, raw_directories: list[Any], raw_files: list[Any], storage_used_bytes: int) -> FileTree:
        """Convert tree JSON into SDK Directory and File objects.

        Each file record includes a containing-directory path, while the API
        does not promise a separate parent object for every file. This method
        uses full directory records when available and otherwise constructs a
        lightweight parent reference carrying the path and authenticated user.

        Args:
            user: Account context that owns the complete tree.
            raw_directories: Directory records returned by the server.
            raw_files: File records returned by the server.
            storage_used_bytes: Validated storage total from the tree response.

        Raises:
            StoreLimitlessResponseError: An entry cannot be converted into a
                supported Directory or File object.
        """
        from .directory import Directory
        from .file import File
        directories: list[Directory] = []
        directories_by_path: dict[str, Directory] = {}
        directories_by_id: dict[int, Directory] = {}
        for record in raw_directories:
            directory = directory_from_payload(user, record)
            directories.append(directory)
            directories_by_path[str(directory.path)] = directory
            directories_by_id[directory.id] = directory
        files: list[File] = []
        for record in raw_files:
            if not isinstance(record, dict):
                raise StoreLimitlessResponseError("StoreLimitless server returned an invalid file-tree entry")
            raw_path: Any = record.get("directory")
            raw_parent_id: Any = record.get("directory_id")
            parent: Directory | None = None
            if isinstance(raw_parent_id, int) and not isinstance(raw_parent_id, bool):
                parent = directories_by_id.get(raw_parent_id)
            if parent is None and isinstance(raw_path, str):
                parent = directories_by_path.get(raw_path)
            if parent is None:
                if not isinstance(raw_path, str) or not raw_path:
                    parent = user.home
                else:
                    parent_id = raw_parent_id if isinstance(raw_parent_id, int) and not isinstance(raw_parent_id, bool) else -1
                    parent = Directory(user, parent_id, Path(raw_path), None, None)
            files.append(File.from_payload(parent, record))
        return cls(directories, files, storage_used_bytes)

    @property
    def active_directories(self) -> list[Directory]:
        """Return directory records whose deleted_at timestamp is empty."""
        return [directory for directory in self.directories if directory.deleted_at is None]

    @property
    def trashed_directories(self) -> list[Directory]:
        """Return directory records currently marked as deleted."""
        return [directory for directory in self.directories if directory.deleted_at is not None]

    @property
    def active_files(self) -> list[File]:
        """Return file records whose deleted_at timestamp is empty."""
        return [file for file in self.files if file.deleted_at is None]

    @property
    def trashed_files(self) -> list[File]:
        """Return file records currently marked as deleted."""
        return [file for file in self.files if file.deleted_at is not None]

    def __repr__(self) -> str:
        """Return counts and storage usage without printing every file."""
        return f"FileTree(directories={len(self.directories)}, files={len(self.files)}, storage_used_bytes={self.storage_used_bytes})"
