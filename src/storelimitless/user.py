"""Account model and object-oriented operations for StoreLimitless.

The User object owns the authentication context used by Directory, File, task,
and job objects. Higher-level methods wrap account-wide operations so callers
can use the SDK's object model instead of assembling raw HTTP requests.
"""

from __future__ import annotations

from os import replace as atomic_replace
from pathlib import Path
from tempfile import NamedTemporaryFile
from shutil import get_terminal_size
from typing import TYPE_CHECKING, Any, Iterable

from requests import Response
from tqdm import tqdm

from .auth import StoreLimitless
from .exception import StoreLimitlessResponseError
from .utils import directory_from_payload
from .tasks import TorrentPreview, UploadJob, UploadTask

if TYPE_CHECKING:
    from .directory import Directory, DirectoryResult
    from .file import File


class User:
    """Represent an authenticated StoreLimitless account.

    The access token is attached automatically to API calls. The password is
    retained in memory only to permit one login retry after an explicit
    invalid-token response; never log or serialize this object. Its home
    directory is a remote virtual directory, normally /home.
    """

    def __init__(self, token: str, username: str, password: str, first_name: str, last_name: str, home: dict[str, int | str | None]) -> None:
        """Initialize a user from a successful login response.

        Applications should normally obtain User instances through
        StoreLimitless.login(). Parameters reflect the server's token, profile
        fields and serialized remote home-directory record.
        """
        self.token: str = token
        self.username: str = username
        self.password: str = password
        self.first_name: str = first_name
        self.last_name: str = last_name
        self.home: Directory = directory_from_payload(self, home)

    def __repr__(self) -> str:
        """Return a developer-friendly representation without credentials."""
        return f"User(username={self.username!r}, first_name={self.first_name!r}, last_name={self.last_name!r}, home={self.home!r})"

    def __str__(self) -> str:
        """Return the user's display name and a readable home-directory summary."""
        home_lines: list[str] = str(self.home).splitlines()
        return f"{self.first_name} {self.last_name} (@{self.username})\n" + "\n".join(home_lines[1:])

    @property
    def metadata(self) -> dict[str, int]:
        """Fetch current storage metadata reported by the server.

        The current backend returns storage_used_bytes, counting both active and
        trashed file bytes. It excludes directory metadata and local computer
        disk usage. Accessing the property makes a fresh HTTP request.
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
        """Return the latest server-reported storage total in bytes."""
        return self.metadata["storage_used_bytes"]

    @property
    def trash(self) -> DirectoryResult:
        """Return trashed directories and files as a DirectoryResult snapshot.

        The trash endpoint does not always include every parent path, so the
        SDK resolves missing file-parent references through active directory
        listings. Use user.tree for a complete account snapshot in one request.
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
                item["directory_id"] for item in raw_files
                if isinstance(item, dict) and isinstance(item.get("directory_id"), int)
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
        """Search file and directory metadata across the entire account.

        The server performs the search globally, rather than limiting it to
        the home/current directory's descendants. Set trash=True to include
        deleted objects.
        """
        return self.home.search(query, trash=trash)

    def move(self, items: Iterable[File | Directory], destination: Directory | str | Path) -> dict[str, Any]:
        """Move multiple files/folders with one request.

        Args:
            items: Non-empty iterable of File and/or Directory objects owned by
                this account.
            destination: Existing remote Directory object or remote path. A
                Directory allows the SDK to check ownership before the request.

        Returns:
            A server result dictionary with moved, unchanged, and failed
            collections. HTTP success does not guarantee that every item moved;
            inspect the failed collection.

        Raises:
            TypeError: An entry is neither a File nor a Directory.
            ValueError: Selection is empty or an object belongs to another user.
            StoreLimitlessResponseError: The response payload is invalid.
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

    @property
    def tree(self) -> FileTree:
        """Fetch the complete account file tree with one API request.

        The snapshot includes active and trashed directories/files; deletion
        state is represented by each object's deleted_at value. FileTree also
        supplies convenience views of active and trashed items. The storage
        total is computed by the server and includes active plus trashed files.
        This is more efficient than recursively traversing Directory.ls.

        File records deliberately omit private provider-link metadata. The tree
        is a point-in-time snapshot; read user.tree again after mutations when
        current information is required.
        """
        from .tree import FileTree

        response = StoreLimitless.request(self, "GET", "/auth/tree")
        try:
            payload: Any = response.json()
            if not isinstance(payload, dict):
                raise TypeError("expected a JSON object")
            raw_directories: Any = payload.get("directories")
            raw_files: Any = payload.get("files")
            storage_used: Any = payload.get("storage_used_bytes")
            if not isinstance(raw_directories, list) or not isinstance(raw_files, list):
                raise TypeError("directories and files must be lists")
            if isinstance(storage_used, bool) or not isinstance(storage_used, int) or storage_used < 0:
                raise TypeError("storage_used_bytes must be a non-negative integer")
            return FileTree.from_payload(self, raw_directories, raw_files, storage_used)
        except (ValueError, TypeError, KeyError) as error:
            raise StoreLimitlessResponseError("StoreLimitless server returned an invalid file-tree response") from error

    def logout(self) -> None:
        """End the current login and revoke links bound to that login session.

        Calls POST /auth/logout. Session-bound file stream/download links and
        ZIP links created during this session become invalid after logout.
        Public links created by File.public_link() are not session-bound and
        are not revoked by this method.

        After success, this object's token is cleared. Do not use this User for
        later authenticated requests; call StoreLimitless.login() for a fresh
        User instance and discard the old object.
        """
        StoreLimitless.request(self, "POST", "/auth/logout")
        self.token = ""

    def preview_torrent(self, link: str) -> TorrentPreview:
        """Fetch torrent file metadata without downloading the torrent payload.

        Args:
            link: Magnet URI containing a supported info-hash (xt) topic.

        Returns:
            TorrentPreview with the torrent name and visible file metadata.
            Each record preserves the original zero-based index, path, and size
            in bytes; retain these indices when selecting files because preview
            rows can omit padding entries.

        Raises:
            ValueError: Link is empty.
            StoreLimitlessResponseError: Response structure is invalid.
            StoreLimitlessHTTPError: Server rejects the link or cannot resolve
                metadata before the configured timeout.
        """
        if not link.strip():
            raise ValueError("Torrent link cannot be empty")
        response = StoreLimitless.request(self, "GET", "/auth/torrent-preview", params={"link": link.strip()}, timeout=150)
        try:
            payload: Any = response.json()
            name: Any = payload.get("name") if isinstance(payload, dict) else None
            files: Any = payload.get("files") if isinstance(payload, dict) else None
            if not isinstance(name, str) or not isinstance(files, list) or not all(isinstance(item, dict) for item in files):
                raise TypeError("name must be a string and files must be an object list")
            return TorrentPreview(name, files)
        except (ValueError, TypeError) as error:
            raise StoreLimitlessResponseError("StoreLimitless server returned an invalid torrent-preview response") from error

    @property
    def upload_tasks(self) -> list[UploadTask]:
        """List shared Task-tray batches and saved task history for this account.

        Every access fetches a fresh list. Since records are Redis-backed, it
        may include work initiated from other signed-in devices. Each UploadTask
        snapshot supports refresh, update, cancel and dismiss; it does not poll
        in the background.
        """
        response = StoreLimitless.request(self, "GET", "/auth/upload-batches")
        try:
            payload: Any = response.json()
            batches: Any = payload.get("batches") if isinstance(payload, dict) else None
            if not isinstance(batches, list) or not all(isinstance(item, dict) for item in batches):
                raise TypeError("batches must be a list of JSON objects")
            return [UploadTask(self, str(item.get("batch_id") or item.get("id") or ""), item) for item in batches if str(item.get("batch_id") or item.get("id") or "").strip()]
        except (ValueError, TypeError) as error:
            raise StoreLimitlessResponseError("StoreLimitless server returned an invalid upload-task list") from error

    def create_upload_task(self, name: str, items: list[str | dict[str, Any]], *, directory: Directory | str | Path | None = None, data_center: str = "Discord", kind: str = "file") -> UploadTask:
        """Register a durable task record for the shared Task tray.

        Creating a task records queue metadata; it does not transfer file bytes
        or start a link import. Use Directory.upload() or the associated import
        workflow to perform the work and UploadTask.update() to publish progress.
        The server accepts 1-2,000 item descriptions. A link task contains one
        item only.

        Args:
            name: Human-readable title shown in the task tray.
            items: Strings or dictionaries describing queued files. Dictionary
                items commonly have a name and optional size in bytes.
            directory: Existing remote Directory or remote path; defaults to home.
            data_center: Server-supported provider, commonly Discord or Telegram.
            kind: file for a file queue or link for one remote import.

        Returns:
            UploadTask wrapper for the new server-side batch.
        """
        from .directory import Directory

        if kind not in {"file", "link"}:
            raise ValueError("kind must be 'file' or 'link'")
        if not items:
            raise ValueError("At least one task item is required")
        if kind == "link" and len(items) != 1:
            raise ValueError("A link task must contain exactly one item")
        if isinstance(directory, Directory):
            if directory.user.username != self.username:
                raise ValueError("Destination directory belongs to another user")
            directory_path = str(directory.path)
        else:
            directory_path = str(directory if directory is not None else self.home.path)

        response = StoreLimitless.request(self, "POST", "/auth/upload-batches", json={"name": name, "kind": kind, "directory": directory_path, "data_center": data_center, "items": items})
        try:
            payload: Any = response.json()
            batch_id: Any = payload.get("batch_id") if isinstance(payload, dict) else None
            if not isinstance(batch_id, str) or not batch_id:
                raise TypeError("batch_id must be a non-empty string")
            seed = {**payload, "name": name, "kind": kind, "directory": directory_path, "data_center": data_center, "items": items}
            return UploadTask(self, batch_id, seed)
        except (ValueError, TypeError) as error:
            raise StoreLimitlessResponseError("StoreLimitless server returned an invalid upload-task creation response") from error

    @property
    def upload_jobs(self) -> list[UploadJob]:
        """List known upload/import jobs and their latest persisted snapshots.

        Each job can be refreshed, cancelled, or dismissed after it is terminal.
        Results may represent work submitted from another signed-in device.
        """
        response = StoreLimitless.request(self, "GET", "/auth/upload-jobs")
        try:
            payload: Any = response.json()
            jobs: Any = payload.get("jobs") if isinstance(payload, dict) else None
            if not isinstance(jobs, list) or not all(isinstance(item, dict) for item in jobs):
                raise TypeError("jobs must be a list of JSON objects")
            result: list[UploadJob] = []
            for item in jobs:
                job_id = item.get("job_id")
                if isinstance(job_id, str) and job_id:
                    result.append(UploadJob(self, job_id, item))
            return result
        except (ValueError, TypeError) as error:
            raise StoreLimitlessResponseError("StoreLimitless server returned an invalid upload-job list") from error

    def get_upload_job(self, job_id: str) -> UploadJob:
        """Create a wrapper and immediately fetch the current state for a job ID."""
        return UploadJob(self, job_id).refresh()

    def create_zip_link(self, items: Iterable[File | Directory]) -> tuple[str, int]:
        """Create a short-lived URL for a streamed ZIP64 archive of selected items.

        Active File and/or Directory objects must belong to this account. Folders
        are included recursively. The server allows at most 500 selected objects
        and requires at least one folder or two files. Archive bytes are streamed
        from remote storage; the complete ZIP is not staged on the server disk.

        Returns:
            (download_url, expires_in_seconds). Current expiry is 900 seconds.
            The URL is session-bound and revoked after logout, but should still
            be treated as a bearer secret.

        Raises:
            ValueError: Empty/duplicate selection, invalid selection size,
                one file without a folder, cross-account item, or unsupported type.
            StoreLimitlessResponseError: Token response is malformed.
        """
        from .directory import Directory
        from .file import File

        file_ids: list[int] = []
        folder_ids: list[int] = []
        seen_files: set[int] = set()
        seen_folders: set[int] = set()
        for item in items:
            if isinstance(item, File):
                if item.user.username != self.username:
                    raise ValueError("Cannot archive a file owned by another user")
                if item.id in seen_files:
                    raise ValueError("Duplicate file selections are not allowed")
                seen_files.add(item.id)
                file_ids.append(item.id)
            elif isinstance(item, Directory):
                if item.user.username != self.username:
                    raise ValueError("Cannot archive a folder owned by another user")
                if item.path == self.home.path:
                    raise ValueError("The home root cannot be selected as an archive folder")
                if item.id in seen_folders:
                    raise ValueError("Duplicate folder selections are not allowed")
                seen_folders.add(item.id)
                folder_ids.append(item.id)
            else:
                raise TypeError("items must contain File and/or Directory objects")
        selected_count = len(file_ids) + len(folder_ids)
        if selected_count < 1 or selected_count > 500 or (not folder_ids and len(file_ids) < 2):
            raise ValueError("Select one folder or at least two files, with at most 500 selected items")

        response = StoreLimitless.request(self, "POST", "/auth/files/zip-link", json={"file_ids": file_ids, "folder_ids": folder_ids})
        try:
            payload: Any = response.json()
            token: Any = payload.get("public_token") if isinstance(payload, dict) else None
            expires_in: Any = payload.get("expires_in") if isinstance(payload, dict) else None
            if not isinstance(token, str) or not token:
                raise TypeError("public_token must be a non-empty string")
            if isinstance(expires_in, bool) or not isinstance(expires_in, int) or expires_in <= 0:
                raise TypeError("expires_in must be a positive integer")
            return f"{StoreLimitless.API_URL}/public/download-zip/{token}", expires_in
        except (ValueError, TypeError) as error:
            raise StoreLimitlessResponseError("StoreLimitless server returned an invalid ZIP-link response") from error

    def download_zip(self, items: Iterable[File | Directory], path: str | Path | None = None, *, filename: str = "StoreLimitless-files.zip", quiet: bool = False) -> Path:
        """Stream selected remote files/folders into a local ZIP64 archive.

        The method creates a session-bound URL and writes archive bytes into a
        temporary sibling file. Once the transfer succeeds, the temporary file
        is atomically renamed to the destination. If download fails, the partial
        file is removed. Folder trees are archived recursively with relative
        paths preserved.

        Args:
            items: Active File and/or Directory objects owned by this account.
            path: Local output path; when omitted, filename is used in the current
                directory. Parent directories are created if missing.
            filename: Suggested archive name passed to the server.
            quiet: When true, suppress the progress bar.

        Returns:
            Path to the fully downloaded archive.
        """
        if not filename.strip():
            raise ValueError("filename cannot be empty")
        url, _expires_in = self.create_zip_link(items)
        output_path = Path(path) if path is not None else Path(filename)
        if output_path.suffix.lower() != ".zip":
            output_path = output_path.with_suffix(".zip")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with NamedTemporaryFile(mode="wb", prefix=f".{output_path.name}.", suffix=".part", dir=output_path.parent, delete=False) as temporary:
                temporary_path = Path(temporary.name)
                response: Response = StoreLimitless.request(self, "GET", url.removeprefix(StoreLimitless.API_URL), params={"filename": Path(filename).name}, stream=True, timeout=(15, 120))
                try:
                    total = max(0, int(response.headers.get("Content-Length", "0") or 0))
                except (TypeError, ValueError):
                    total = 0
                with tqdm(total=total, unit="B", unit_scale=True, unit_divisor=1024, desc=output_path.name, disable=quiet, ncols=get_terminal_size(fallback=(80, 24)).columns, bar_format="{desc} {bar} {n_fmt}/{total_fmt} {rate_fmt} ETA {remaining}") as progress:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            temporary.write(chunk)
                            progress.update(len(chunk))
            atomic_replace(str(temporary_path), str(output_path))
            return output_path
        except Exception:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise
