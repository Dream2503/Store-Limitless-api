"""Typed object model for the StoreLimitless whole-account file-tree endpoint.

The backend /auth/tree endpoint returns all active and trashed objects in one
response. FileTree preserves that snapshot as SDK Directory and File objects
and exposes active/deleted partitions and the server-computed storage total.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from .exception import StoreLimitlessResponseError
from .utils import directory_from_payload, parse_datetime

if TYPE_CHECKING:
    from .directory import Directory
    from .file import File
    from .user import User


class FileTree:
    """Represent the complete snapshot returned by GET /auth/tree.

    Attributes:
        directories: All directory objects, active and trashed.
        files: All file objects, active and trashed.
        storage_used_bytes: Backend-computed storage total, including active
            and trashed file bytes. This value is not recalculated locally.

    Individual Directory and File objects indicate deletion with deleted_at.
    This is a point-in-time snapshot and will not update itself after mutations.
    """

    def __init__(self, directories: list[Directory], files: list[File], storage_used_bytes: int) -> None:
        """Build a tree snapshot from SDK objects and server-reported usage."""
        self.directories: list[Directory] = directories
        self.files: list[File] = files
        self.storage_used_bytes: int = storage_used_bytes

    @classmethod
    def from_payload(cls, user: User, raw_directories: list[Any], raw_files: list[Any], storage_used_bytes: int) -> FileTree:
        """Convert the server's JSON tree to SDK objects.

        File records include a containing-directory path; directory objects
        may not be provided inline. This method reuses complete directory
        records when available and otherwise builds lightweight parent
        references carrying the path and authenticated user context.

        Args:
            user: Authenticated account owning this tree.
            raw_directories: Directory records returned by the tree endpoint.
            raw_files: File records returned by the tree endpoint.
            storage_used_bytes: Validated storage total returned by the server.

        Raises:
            StoreLimitlessResponseError: Any record cannot be converted to a
                supported Directory or File model.
        """
        from .directory import Directory
        from .file import File

        directories: list[Directory] = []
        directories_by_path: dict[str, Directory] = {}
        directories_by_id: dict[int, Directory] = {}
        for record in raw_directories:
            if not isinstance(record, dict):
                raise StoreLimitlessResponseError("StoreLimitless server returned an invalid directory-tree entry")
            normalized_record = dict(record)
            is_deleted = normalized_record.get("is_deleted") is True
            if is_deleted and not normalized_record.get("deleted_at"):
                normalized_record["deleted_at"] = "1970-01-01T00:00:00+00:00"
            directory = directory_from_payload(user, normalized_record)
            directories.append(directory)
            directories_by_path[str(directory.path)] = directory
            directories_by_id[directory.id] = directory

        files: list[File] = []
        for record in raw_files:
            if not isinstance(record, dict):
                raise StoreLimitlessResponseError("StoreLimitless server returned an invalid file-tree entry")
            normalized_record = dict(record)
            is_deleted = normalized_record.get("is_deleted") is True
            if is_deleted and not normalized_record.get("deleted_at"):
                normalized_record["deleted_at"] = "1970-01-01T00:00:00+00:00"
            raw_path: Any = normalized_record.get("directory")
            raw_parent_id: Any = normalized_record.get("directory_id")
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
            file = File.from_payload(parent, normalized_record)
            if is_deleted and file.deleted_at is None:
                file.deleted_at = parse_datetime("1970-01-01T00:00:00+00:00")
            files.append(file)

        return cls(directories, files, storage_used_bytes)

    @property
    def active_directories(self) -> list[Directory]:
        """Return directory records whose deleted_at value is empty."""
        return [directory for directory in self.directories if directory.deleted_at is None]

    @property
    def trashed_directories(self) -> list[Directory]:
        """Return directory records currently marked as deleted."""
        return [directory for directory in self.directories if directory.deleted_at is not None]

    @property
    def active_files(self) -> list[File]:
        """Return file records whose deleted_at value is empty."""
        return [file for file in self.files if file.deleted_at is None]

    @property
    def trashed_files(self) -> list[File]:
        """Return file records currently marked as deleted."""
        return [file for file in self.files if file.deleted_at is not None]

    def __repr__(self) -> str:
        """Return counts and storage usage without printing every file record."""
        return f"FileTree(directories={len(self.directories)}, files={len(self.files)}, storage_used_bytes={self.storage_used_bytes})"
