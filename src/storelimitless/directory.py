"""Directory models and operations exposed by the StoreLimitless SDK."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from shutil import get_terminal_size
from time import monotonic, sleep
from typing import TYPE_CHECKING, Any, Iterator, Literal, cast

from requests import Response
from tqdm import tqdm

from .auth import StoreLimitless
from .exception import StoreLimitlessConnectionError, StoreLimitlessHTTPError, StoreLimitlessResponseError
from .file import File
from .utils import directory_from_payload

if TYPE_CHECKING:
    from .user import User


class DirectoryResult:
    """A pair of file and directory collections returned by a listing or search.

    Iteration intentionally yields ``directories`` first and ``files`` second, preserving the existing ``directories, files = result`` interface.
    """

    def __init__(self, directories: list[Directory], files: list[File]) -> None:
        self.directories: list[Directory] = directories
        self.files: list[File] = files

    def __iter__(self) -> Iterator[list[Directory] | list[File]]:
        """Yield the directory list followed by the file list."""

        yield self.directories
        yield self.files

    @property
    def directory(self) -> Directory | None:
        """Return the first matching directory, or ``None`` if there is none."""

        return self.directories[0] if self.directories else None

    @property
    def file(self) -> File | None:
        """Return the first matching file, or ``None`` if there is none."""

        return self.files[0] if self.files else None


class Directory:
    """Represent a user-owned directory and provide remote filesystem operations.

    ``modified_at`` may be ``None`` for a lightweight parent-directory reference returned by global search, where the API provides the parent's path
    and ID but not its own timestamps.
    """

    def __init__(self, user: User, id: int, path: Path, modified_at: datetime | None, deleted_at: datetime | None) -> None:
        self.user: User = user
        self.id: int = id
        self.path: Path = Path(path)
        self.modified_at: datetime | None = modified_at
        self.deleted_at: datetime | None = deleted_at

    def __repr__(self) -> str:
        """Return a developer-oriented representation of this directory."""

        return (
            f"Directory(id={self.id}, path={self.path!r}, "
            f"modified_at={self.modified_at!r}, deleted_at={self.deleted_at!r}, "
            f"user={self.user.username!r})"
        )

    def __str__(self) -> str:
        """Return a human-readable summary of this directory's contents."""

        folders: int = 0
        files_count: int = 0
        total_size: int = 0
        directories_to_visit: list[Directory] = [self]

        while directories_to_visit:
            directory: Directory = directories_to_visit.pop()
            children, files = directory.ls
            folders += len(children)
            files_count += len(files)
            total_size += sum(file.size for file in files)
            directories_to_visit.extend(cast(list[Directory], children))

        unit: str = "B"
        size: int | float = float(total_size)
        units: tuple[str, ...] = ("B", "KB", "MB", "GB", "TB")

        for unit in units:
            if size < 1024 or unit == units[-1]:
                break

            size /= 1024

        modified: str = self.modified_at.isoformat() if self.modified_at else "unknown"
        return (
            f"Directory: {self.path}\n"
            f"Folders: {folders}\n"
            f"Files: {files_count}\n"
            f"Size: {size:.2f} {unit}\n"
            f"Modified: {modified}\n"
        )

    def __truediv__(self, arg: str) -> Directory:
        """Allow navigation with ``directory / "child"`` syntax."""

        return self.cd(arg)

    def mkdir(self, name: str) -> Directory:
        """Create a child directory and return its refreshed SDK object."""

        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise ValueError("Invalid folder name")

        StoreLimitless.request(self.user, "POST", "/auth/create-folder", params={"directory": str(self.path), "name": name})
        created_path: Path = self.path / name

        for directory in self.ls.directories:
            if directory.path == created_path:
                return directory

        raise StoreLimitlessResponseError("Folder was created, but it was not present in the refreshed directory listing")

    def cd(self, arg: str) -> Directory:
        """Return a child, parent, or home directory without changing this object."""

        if arg in {"~", "/"}:
            return self.user.home

        if arg == "..":
            if self.path == self.user.home.path:
                return self.user.home

            relative_parent: Path = self.path.parent.relative_to(self.user.home.path)
            directory: Directory = self.user.home

            for part in relative_parent.parts:
                directory = directory / part

            return directory

        if len(Path(arg).parts) != 1 or arg in {".", ".."}:
            raise ValueError("cd accepts only one directory name at a time")

        wanted_path: Path = self.path / arg

        for directory in self.ls.directories:
            if directory.path == wanted_path:
                return directory

        raise ValueError(f"Directory does not exist: {wanted_path}")

    @property
    def ls(self) -> DirectoryResult:
        """List immediate child folders and files in this directory."""

        response: Response = StoreLimitless.request(self.user, "GET", "/auth/directory", params={"directory": str(self.path)})

        try:
            raw_directories, raw_files = response.json()

            if not isinstance(raw_directories, list) or not isinstance(raw_files, list):
                raise TypeError("Expected two arrays")

            directories: list[Directory] = [directory_from_payload(self.user, item) for item in raw_directories]
            files: list[File] = [File.from_payload(self, item) for item in raw_files]
            return DirectoryResult(directories, files)

        except (ValueError, KeyError, TypeError) as error:
            if isinstance(error, StoreLimitlessResponseError):
                raise

            raise StoreLimitlessResponseError("StoreLimitless server returned an invalid directory listing") from error

    def search(self, query: str, *, trash: bool = False) -> DirectoryResult:
        """Search all the user's folders and files by name on the server.

        The backend search is global rather than limited to this directory's descendants. Pass ``trash=True`` to search items currently in the trash.
        """
        if not query or not query.strip():
            raise ValueError("Search query cannot be empty")

        response: Response = StoreLimitless.request(self.user, "GET", "/auth/search", params={"query": query.strip(), "trash": trash})

        try:
            data: Any = response.json()

            if not isinstance(data, dict):
                raise TypeError("Expected an object")

            raw_directories: Any = data.get("directories", [])
            raw_files: Any = data.get("files", [])

            if not isinstance(raw_directories, list) or not isinstance(raw_files, list):
                raise TypeError("Expected directories and files arrays")

            directories: list[Directory] = [directory_from_payload(self.user, item) for item in raw_directories]
            files: list[File] = []

            for item in raw_files:
                if not isinstance(item, dict):
                    raise TypeError("Expected a file object")

                directory_path: str = str(item.get("directory") or self.path)
                directory_id_raw: Any = item.get("directory_id")
                directory_id: int = directory_id_raw if isinstance(directory_id_raw, int) else -1
                parent = Directory(self.user, directory_id, Path(directory_path), None, None)
                files.append(File.from_payload(parent, item))

            return DirectoryResult(directories, files)

        except (ValueError, KeyError, TypeError) as error:
            if isinstance(error, StoreLimitlessResponseError):
                raise

            raise StoreLimitlessResponseError("StoreLimitless server returned an invalid search response") from error

    def find(self, file_name: str, *, trash: bool = False) -> DirectoryResult:
        """Compatibility alias for :meth:`search`.

        Unlike the older client-side recursive implementation, search is now delegated to the backend and searches the whole account.
        """

        return self.search(file_name, trash=trash)

    def rename(self, name: str) -> Directory:
        """Rename this directory, including its descendant paths, on the server."""

        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise ValueError("Invalid folder name")

        response: Response = StoreLimitless.request(self.user, "PATCH", f"/auth/directory/{self.id}/rename", params={"name": name})

        try:
            data: Any = response.json()
            new_path: Any = data.get("path") if isinstance(data, dict) else None
            self.path = Path(new_path) if isinstance(new_path, str) else self.path.with_name(name)

            if isinstance(data, dict) and isinstance(data.get("name"), str):
                self.path = self.path.with_name(data["name"])

            return self

        except (ValueError, TypeError) as error:
            raise StoreLimitlessResponseError("StoreLimitless server returned an invalid directory rename response") from error

    def move(self, destination: Directory | str | Path) -> Directory:
        """Move this directory into an existing destination directory."""

        if isinstance(destination, Directory):
            if destination.user.username != self.user.username:
                raise ValueError("Cannot move a directory to another user's filesystem")

            destination_path: str = str(destination.path)

        else:
            destination_path = str(destination)

        response: Response = StoreLimitless.request(
            self.user,
            "PATCH",
            f"/auth/directory/{self.id}/move",
            params={"directory": destination_path}
        )

        try:
            data: Any = response.json()
            new_path: Any = data.get("path") if isinstance(data, dict) else None

            if isinstance(new_path, str):
                self.path = Path(new_path)

            elif isinstance(destination, Directory):
                self.path = destination.path / self.path.name

            else:
                self.path = Path(destination_path) / self.path.name

            return self

        except (ValueError, TypeError) as error:
            raise StoreLimitlessResponseError("StoreLimitless server returned an invalid directory move response") from error

    def upload(
            self,
            source: str | Path,
            data_center: Literal["Discord", "Telegram"] = "Discord",
            *,
            quiet: bool = False,
            chunk_size: int = 10 * 1024 * 1024,
            chunk_retries: int = 3,
            poll_interval: int | float = 0.2,
            job_timeout: int | float | None = 3600.0,
    ) -> File:
        """Upload a local file in chunks or import a file from an HTTP(S) URL.

        Local file uploads first create a job through ``POST /auth/upload`` and then use the authenticated ``PUT /public/upload/{job_id}/chunk``
        route. URL imports use ``POST /auth/upload-link``. Both flows poll the public upload status endpoint and return the final :class:`File`
        object.

        Args:
            source: A local path or HTTP(S) source URL.
            data_center: Storage provider to use, currently ``Discord`` or ``Telegram``.
            quiet: Disable the terminal progress bar.
            chunk_size: Chunk size in bytes for local uploads; must be positive.
            chunk_retries: Number of attempts for a transient failed chunk.
            poll_interval: Seconds between status checks.
            job_timeout: Maximum seconds to wait for a job; ``None`` waits indefinitely.

        Raises:
            FileNotFoundError: The local source does not exist.
            ValueError: An invalid chunk size or retry count was supplied.
            StoreLimitlessHTTPError: An API endpoint rejected the request.
            StoreLimitlessResponseError: A response was malformed or the upload failed.
        """
        if chunk_size <= 0:
            raise ValueError("chunk_size must be greater than zero")

        if chunk_retries < 1:
            raise ValueError("chunk_retries must be at least 1")

        if poll_interval < 0:
            raise ValueError("poll_interval cannot be negative")

        is_link: bool = isinstance(source, str) and source.lower().startswith(("http://", "https://"))
        local_path: Path | None = None
        total_bytes: int = 0

        if is_link:
            response = StoreLimitless.request(
                self.user,
                "POST",
                "/auth/upload-link",
                params={"link": source, "data_center": data_center, "directory": str(self.path)},
                timeout=60,
            )

        else:
            local_path: Path = Path(source).expanduser()

            if not local_path.is_file():
                raise FileNotFoundError(f"Upload source is not a file: {local_path}")

            total_bytes = local_path.stat().st_size
            response = StoreLimitless.request(
                self.user,
                "POST",
                "/auth/upload",
                headers={
                    "X-Data-Center": data_center,
                    "X-File-Name": local_path.name,
                    "X-Directory": str(self.path),
                },
                timeout=60,
            )

        try:
            response_data: Any = response.json()
            job_id: Any = response_data["job_id"] if isinstance(response_data, dict) else None

            if not isinstance(job_id, str) or not job_id:
                raise TypeError("job_id must be a non-empty string")

        except (ValueError, KeyError, TypeError) as error:
            raise StoreLimitlessResponseError("StoreLimitless server returned an invalid upload response") from error

        if local_path is not None:
            with local_path.open("rb") as stream:
                offset: int = 0

                while True:
                    chunk: bytes = stream.read(chunk_size)

                    if not chunk and not (total_bytes == 0 and offset == 0):
                        break

                    for attempt in range(1, chunk_retries + 1):
                        try:
                            StoreLimitless.request(
                                self.user,
                                "PUT",
                                f"/public/upload/{job_id}/chunk",
                                params={"offset": offset, "total": total_bytes},
                                headers={"Content-Type": "application/octet-stream"},
                                data=chunk,
                                timeout=120,
                            )
                            break

                        except StoreLimitlessConnectionError:
                            if attempt >= chunk_retries:
                                raise

                            sleep(min(float(attempt), 3.0))

                        except StoreLimitlessHTTPError as error:
                            if error.status_code < 500 or attempt >= chunk_retries:
                                raise

                            sleep(min(float(attempt), 3.0))

                    offset += len(chunk)

                    if not chunk:
                        break

        progress_bar = tqdm(
            total=0,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            disable=quiet,
            ascii=" ━",
            colour="green",
            ncols=get_terminal_size(fallback=(80, 24)).columns,
            bar_format="{desc} {bar} {n_fmt}/{total_fmt} {rate_fmt} ETA {remaining}",
        )
        started_at: float = monotonic()

        try:
            while True:
                if job_timeout is not None and monotonic() - started_at > job_timeout:
                    raise StoreLimitlessConnectionError(f"Upload job {job_id} timed out while waiting for completion")

                status_response: Response = StoreLimitless.request(self.user, "GET", f"/public/upload/{job_id}/status", timeout=30)

                try:
                    raw_status: Any = status_response.json()

                    if not isinstance(raw_status, dict):
                        raise TypeError("Expected an object")

                    final_status: dict = raw_status
                    total: int = int(raw_status.get("total", 0) or 0)
                    transfer: int = int(raw_status.get("transfer", 0) or 0)
                    progress_bar.total = max(0, total)
                    progress_bar.n = min(max(0, transfer), progress_bar.total) if progress_bar.total else max(0, transfer)
                    progress_bar.set_description(str(raw_status.get("message") or "Uploading"), refresh=False)
                    progress_bar.refresh()
                    status_value: str = str(raw_status.get("status", "uploading")).lower()

                except (ValueError, TypeError) as error:
                    raise StoreLimitlessResponseError("StoreLimitless server returned an invalid upload status") from error

                if status_value == "completed":
                    break

                if status_value == "error":
                    raise StoreLimitlessResponseError(str(raw_status.get("message") or f"Upload job {job_id} failed"))

                sleep(poll_interval)

        finally:
            progress_bar.close()

        if final_status is None:
            raise StoreLimitlessResponseError("StoreLimitless server returned no upload status")

        file_id_raw: Any = final_status.get("file_id")

        if isinstance(file_id_raw, bool) or not isinstance(file_id_raw, int):
            raise StoreLimitlessResponseError("Upload completed but the server did not return a file ID")

        for file in self.ls.files:
            if file.id == file_id_raw:
                return file

        raise StoreLimitlessResponseError(f"Upload completed but file {file_id_raw} could not be found in {self.path}")

    def rm(self) -> Directory:
        """Move this directory to the trash and return this object."""

        StoreLimitless.request(self.user, "DELETE", f"/auth/directory/{self.id}")
        self.deleted_at = datetime.now(timezone.utc)
        return self

    def delete(self) -> None:
        """Permanently delete this directory and its trashed contents."""

        StoreLimitless.request(self.user, "DELETE", f"/auth/trash/directory/{self.id}")

    def restore(self) -> Directory:
        """Restore this directory from the trash."""

        StoreLimitless.request(self.user, "POST", f"/auth/trash/directory/{self.id}/restore")
        self.deleted_at = None
        return self
