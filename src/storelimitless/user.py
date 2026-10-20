"""Authenticated user model and account-level SDK operations."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from .auth import StoreLimitless
from .exception import StoreLimitlessResponseError
from .utils import directory_from_payload

if TYPE_CHECKING:
    from .directory import Directory, DirectoryResult
    from .file import File


class User:
    """Represent an authenticated StoreLimitless account.

    ``token`` is attached to subsequent API requests. The password is retained in memory to support one automatic re-login if the backend explicitly
    responds with ``Invalid token``; do not log or serialize this object.
    """

    def __init__(self, token: str, username: str, password: str, first_name: str, last_name: str, home: dict[str, int | str | None]) -> None:
        self.token: str = token
        self.username: str = username
        self.password: str = password
        self.first_name: str = first_name
        self.last_name: str = last_name
        self.home: Directory = directory_from_payload(self, home)

    def __repr__(self) -> str:
        """Return a safe representation that does not expose token/password."""

        return f"User(username={self.username!r}, first_name={self.first_name!r}, last_name={self.last_name!r}, home={self.home!r})"

    def __str__(self) -> str:
        """Return the user's display name and home-directory summary."""

        home_lines: list[str] = str(self.home).splitlines()
        return f"{self.first_name} {self.last_name} (@{self.username})\n" + "\n".join(home_lines[1:])

    @property
    def metadata(self) -> dict[str, int]:
        """Fetch account metadata, including storage used in bytes.

        The current backend's ``storage_used_bytes`` value counts both active and trashed files. It does not count directory metadata as storage.
        """
        response = StoreLimitless.request(self, "GET", "/auth/metadata")

        try:
            data: Any = response.json()
            storage_used: Any = data.get("storage_used_bytes") if isinstance(data, dict) else None

            if isinstance(storage_used, bool) or not isinstance(storage_used, int):
                raise TypeError("storage_used_bytes must be an integer")

            return {"storage_used_bytes": storage_used}

        except (ValueError, TypeError) as error:
            raise StoreLimitlessResponseError("StoreLimitless server returned invalid account metadata") from error

    @property
    def storage_used_bytes(self) -> int:
        """Return the current storage usage in bytes."""

        return self.metadata["storage_used_bytes"]

    @property
    def trash(self) -> DirectoryResult:
        """Return trashed directories and files for this user.

        Parent directories for trashed files are resolved from the trash listing first, then (when needed) from active directory listings. The API's
        trash endpoint returns file ``directory_id`` values but not each file's parent path directly.
        """

        from .directory import Directory, DirectoryResult
        from .file import File

        response = StoreLimitless.request(self, "GET", "/auth/trash")

        try:
            raw_directories, raw_files = response.json()

            if not isinstance(raw_directories, list) or not isinstance(raw_files, list):
                raise TypeError("Expected two arrays")

            directories: list[Directory] = [directory_from_payload(self, item) for item in raw_directories]
            directory_by_id: dict[int, Directory] = {directory.id: directory for directory in directories}
            directory_by_id.setdefault(self.home.id, self.home)
            needed_ids: set[int] = {
                item["directory_id"]
                for item in raw_files
                if isinstance(item, dict)
                   and isinstance(item.get("directory_id"), int)
                   and item["directory_id"] not in directory_by_id
            }

            if needed_ids:
                pending: list[Directory] = [self.home]
                visited: set[int] = set()

                while pending and not needed_ids.issubset(directory_by_id.keys()):
                    current: Directory = pending.pop()

                    if current.id in visited:
                        continue

                    visited.add(current.id)
                    children: list[Directory] = current.ls.directories

                    for child in children:
                        directory_by_id[child.id] = child

                    pending.extend(children)

            files: list[File] = []

            for item in raw_files:
                if not isinstance(item, dict):
                    raise TypeError("Expected a file object")

                parent_id: Any = item.get("directory_id")
                parent: Directory = directory_by_id.get(parent_id, self.home) if isinstance(parent_id, int) else self.home
                files.append(File.from_payload(parent, item))

            return DirectoryResult(directories, files)

        except (ValueError, KeyError, TypeError) as error:
            if isinstance(error, StoreLimitlessResponseError):
                raise

            raise StoreLimitlessResponseError("StoreLimitless server returned an invalid trash response") from error

    def search(self, query: str, *, trash: bool = False) -> DirectoryResult:
        """Search account-wide for files and directories by name."""

        return self.home.search(query, trash=trash)

    def move(self, items: Iterable[File | Directory], destination: Directory | str | Path) -> dict[str, Any]:
        """Move multiple files/folders to a destination using one bulk request.

        The backend reports successful moves, unchanged items, and per-item failures separately. A successful HTTP response can therefore still
        contain entries in the returned ``failed`` array.
        """

        from .directory import Directory
        from .file import File

        if isinstance(destination, Directory):
            if destination.user.username != self.username:
                raise ValueError("Destination directory belongs to another user")

            destination_path: str = str(destination.path)

        else:
            destination_path = str(destination)

        payload_items: list[dict[str, str | int]] = []

        for item in items:
            if isinstance(item, File):
                item_user = item.user
                item_type = "file"

            elif isinstance(item, Directory):
                item_user = item.user
                item_type = "folder"

            else:
                raise TypeError("items must contain File or Directory objects")

            if item_user.username != self.username:
                raise ValueError("Cannot move items owned by another user")

            payload_items.append({"type": item_type, "id": item.id})

        if not payload_items:
            raise ValueError("At least one file or folder must be selected")

        response = StoreLimitless.request(self, "PATCH", "/auth/move", json={"directory": destination_path, "items": payload_items})

        try:
            data: Any = response.json()

            if not isinstance(data, dict):
                raise TypeError("Expected a JSON object")

            return data

        except (ValueError, TypeError) as error:
            raise StoreLimitlessResponseError("StoreLimitless server returned an invalid bulk move response") from error
