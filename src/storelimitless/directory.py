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
from .tasks import UploadJob
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

    def import_link(
            self,
            link: str,
            data_center: Literal["Discord", "Telegram"] = "Discord",
            *,
            batch_id: str | None = None,
            selected_files: list[int] | None = None,
    ) -> UploadJob:
        """Start an asynchronous HTTP(S), YouTube, Telegram, or torrent import.

        The server performs the ingestion in the background. Refresh the
        returned UploadJob to observe progress. For a torrent magnet, pass
        original zero-based file indices from User.preview_torrent(); omitted
        indices mean import every file in the torrent.
        """
        from json import dumps

        raw_link = link.strip()
        if not raw_link:
            raise ValueError("Import link cannot be empty")

        is_magnet = raw_link.lower().startswith("magnet:")
        if is_magnet:
            if not raw_link.lower().startswith("magnet:?"):
                raise ValueError("Torrent links must use the magnet:? form")
        elif not raw_link.lower().startswith(("http://", "https://")):
            raise ValueError("Import link must be an HTTP(S) URL or torrent magnet URI")

        if selected_files is not None:
            if not is_magnet:
                raise ValueError("selected_files can only be used with a torrent magnet URI")
            if not selected_files:
                raise ValueError("Select at least one torrent file")
            if any(isinstance(index, bool) or not isinstance(index, int) or index < 0 for index in selected_files):
                raise ValueError("Torrent file indices must be non-negative integers")
            if len(set(selected_files)) != len(selected_files):
                raise ValueError("Duplicate torrent file indices are not allowed")

        if batch_id is not None and not batch_id.strip():
            raise ValueError("batch_id cannot be empty")

        params = {
            "link": raw_link,
            "data_center": data_center,
            "directory": str(self.path),
            "batch_id": batch_id or "",
            "selected_files": dumps(selected_files) if selected_files is not None else "",
        }
        response = StoreLimitless.request(
            self.user, "POST", "/auth/upload-link", params=params, timeout=60
        )
        try:
            payload: Any = response.json()
            job_id: Any = payload.get("job_id") if isinstance(payload, dict) else None
            if not isinstance(job_id, str) or not job_id:
                raise TypeError("job_id must be a non-empty string")
            return UploadJob(
                self.user,
                job_id,
                {
                    "job_id": job_id,
                    "name": raw_link,
                    "status": "uploading",
                    "message": "Import queued",
                    "ingestion_type": "torrent" if is_magnet else "",
                },
            )
        except (ValueError, TypeError) as error:
            raise StoreLimitlessResponseError(
                "StoreLimitless server returned an invalid link-import response"
            ) from error

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
        """Upload one local file or synchronously import one HTTP(S) resource.

        Local files use authenticated chunk uploads. HTTP(S) URLs use the
        server-side import-job API and wait until the job completes. Use
        ``import_link()`` when you want a non-blocking job object, a torrent
        magnet, selected torrent files, or an import that may create multiple
        files.
        """
        if chunk_size <= 0:
            raise ValueError("chunk_size must be greater than zero")
        if chunk_retries < 1:
            raise ValueError("chunk_retries must be at least 1")
        if poll_interval < 0:
            raise ValueError("poll_interval cannot be negative")
        if job_timeout is not None and job_timeout < 0:
            raise ValueError("job_timeout cannot be negative")

        is_link = isinstance(source, str) and source.lower().startswith(("http://", "https://"))
        is_magnet = isinstance(source, str) and source.lower().startswith("magnet:")
        if is_magnet:
            raise ValueError("Use Directory.import_link() for torrent magnet URIs")

        if is_link:
            existing_ids = {file.id for file in self.ls.files}
            job = self.import_link(source, data_center=data_center)
            started_at = monotonic()

            while True:
                if job_timeout is not None and monotonic() - started_at > job_timeout:
                    raise StoreLimitlessConnectionError(
                        f"Import job {job.job_id} timed out while waiting for completion"
                    )
                job.refresh()
                status = job.status.lower()
                if status == "completed":
                    break
                if status in {"error", "cancelled"}:
                    raise StoreLimitlessResponseError(
                        job.message or f"Import job {job.job_id} {status}"
                    )
                sleep(poll_interval)

            new_files = [file for file in self.ls.files if file.id not in existing_ids]
            if len(new_files) == 1:
                return new_files[0]

            matching = [file for file in new_files if file.name == job.name]
            if len(matching) == 1:
                return matching[0]

            raise StoreLimitlessResponseError(
                "Import completed, but it did not produce exactly one identifiable file. "
                "Use Directory.import_link() and inspect the resulting job and directory listing "
                "for multi-file imports."
            )

        local_path = Path(source).expanduser()
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
            job_id: Any = response_data.get("job_id") if isinstance(response_data, dict) else None
            if not isinstance(job_id, str) or not job_id:
                raise TypeError("job_id must be a non-empty string")
        except (ValueError, TypeError) as error:
            raise StoreLimitlessResponseError("StoreLimitless server returned an invalid upload response") from error

        with local_path.open("rb") as stream:
            offset = 0
            while True:
                chunk = stream.read(chunk_size)
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
        started_at = monotonic()
        final_status: dict[str, Any] | None = None

        try:
            while True:
                if job_timeout is not None and monotonic() - started_at > job_timeout:
                    raise StoreLimitlessConnectionError(
                        f"Upload job {job_id} timed out while waiting for completion"
                    )

                status_response = StoreLimitless.request(
                    self.user, "GET", f"/public/upload/{job_id}/status", timeout=30
                )
                try:
                    raw_status: Any = status_response.json()
                    if not isinstance(raw_status, dict):
                        raise TypeError("Expected an object")
                    final_status = raw_status
                    total = int(raw_status.get("total", 0) or 0)
                    transfer = int(raw_status.get("transfer", 0) or 0)
                    progress_bar.total = max(0, total)
                    progress_bar.n = min(max(0, transfer), progress_bar.total) if progress_bar.total else max(0, transfer)
                    progress_bar.set_description(str(raw_status.get("message") or "Uploading"), refresh=False)
                    progress_bar.refresh()
                    status_value = str(raw_status.get("status", "uploading")).lower()
                except (ValueError, TypeError) as error:
                    raise StoreLimitlessResponseError(
                        "StoreLimitless server returned an invalid upload status"
                    ) from error

                if status_value == "completed":
                    break
                if status_value in {"error", "cancelled"}:
                    raise StoreLimitlessResponseError(
                        str(raw_status.get("message") or f"Upload job {job_id} {status_value}")
                    )
                sleep(poll_interval)
        finally:
            progress_bar.close()

        if final_status is None:
            raise StoreLimitlessResponseError("StoreLimitless server returned no upload status")

        file_id = final_status.get("file_id")
        if isinstance(file_id, bool) or not isinstance(file_id, int):
            raise StoreLimitlessResponseError(
                "Upload completed but the server did not return a file ID"
            )

        for file in self.ls.files:
            if file.id == file_id:
                return file
        raise StoreLimitlessResponseError(
            f"Upload completed but file {file_id} could not be found in {self.path}"
        )

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
