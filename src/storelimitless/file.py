"""File model and operations for remote StoreLimitless files.

A File contains a point-in-time snapshot of one remote file's metadata. It
keeps a reference to its parent Directory, which in turn provides the
authenticated User context for API requests. Public links, session-bound links,
downloads and media playback are exposed as object methods.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from shutil import get_terminal_size
from subprocess import Popen
from typing import TYPE_CHECKING, Any

from requests import Response
from tqdm import tqdm

from .auth import StoreLimitless
from .exception import StoreLimitlessConnectionError, StoreLimitlessResponseError
from .utils import ensure_vlc, parse_datetime

if TYPE_CHECKING:
    from .directory import Directory
    from .user import User


class File:
    """Represent a stored remote file and its metadata.

    Attributes such as name, size and deleted_at describe the object when the
    File was created or last mutated through this object. Independently changed
    server state does not update the instance automatically. A File belongs to
    a parent Directory and uses that directory's User for authenticated
    operations. It is not a local pathlib.Path.
    """

    def __init__(
        self,
        directory: Directory,
        id: int,
        name: str,
        type: str,
        size: int,
        modified_at: datetime,
        deleted_at: datetime | None,
        data_center: str,
    ) -> None:
        """Initialize a File snapshot using already parsed metadata.

        Applications typically obtain File objects from Directory.ls, search,
        trash, an upload result, or User.tree rather than constructing them
        directly. The directory stores the authenticated account context.
        """
        self.directory: Directory = directory
        self.id: int = id
        self.name: str = name
        self.type: str = type
        self.size: int = size
        self.modified_at: datetime = modified_at
        self.deleted_at: datetime | None = deleted_at
        self.data_center: str = data_center

    @classmethod
    def from_payload(cls, directory: Directory, payload: dict[str, Any]) -> File:
        """Validate a server file record and convert it into a File snapshot.

        Args:
            directory: Parent Directory object used to retain the account context.
            payload: JSON-compatible server record. Required values are id,
                name, type, size, modified_at, and data_center; deleted_at may
                be absent or null.

        Raises:
            StoreLimitlessResponseError: Any required field is missing or has an
                invalid type, or a timestamp cannot be parsed.
        """
        try:
            file_id: Any = payload["id"]
            name: Any = payload["name"]
            file_type: Any = payload["type"]
            size: Any = payload["size"]
            data_center: Any = payload["data_center"]
            modified_at = parse_datetime(payload.get("modified_at"))
            deleted_at = parse_datetime(payload.get("deleted_at"))

            if isinstance(file_id, bool) or not isinstance(file_id, int):
                raise TypeError("id must be an integer")
            if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                raise TypeError("size must be a non-negative integer")
            if not isinstance(name, str) or not isinstance(file_type, str) or not isinstance(data_center, str):
                raise TypeError("invalid string field")
            if modified_at is None:
                raise TypeError("modified_at is missing")

            return cls(directory, file_id, name, file_type, size, modified_at, deleted_at, data_center)

        except (KeyError, TypeError) as error:
            if isinstance(error, StoreLimitlessResponseError):
                raise
            raise StoreLimitlessResponseError("StoreLimitless server returned an invalid file object") from error

    def __repr__(self) -> str:
        """Return a developer-oriented representation of this file."""
        return (
            f"File(directory={self.directory!r}, id={self.id}, name={self.name!r}, "
            f"type={self.type!r}, size={self.size}, modified_at={self.modified_at!r}, "
            f"deleted_at={self.deleted_at!r}, data_center={self.data_center!r})"
        )

    def __str__(self) -> str:
        """Return a human-readable file summary with a scaled size."""
        size: int | float = float(self.size)
        units: tuple[str, ...] = ("B", "KB", "MB", "GB", "TB")
        unit: str = units[0]
        for unit in units:
            if size < 1024 or unit == units[-1]:
                break
            size /= 1024
        return (
            f"File: {self.name}\n"
            f"Path: {self.directory.path}\n"
            f"Size: {size:.2f} {unit}\n"
            f"Modified: {self.modified_at}\n"
            f"Data Center: {self.data_center}\n"
        )

    @property
    def user(self) -> User:
        """Return the authenticated account associated with this file."""
        return self.directory.user

    def public_link(self, *, download: bool = False) -> str:
        """Create a public stream or download URL for this file.

        The URL is a bearer link: anyone who obtains it may be able to access
        the file while the server accepts the token. Use it only when access
        outside the current login session is intended.

        Args:
            download: Return an attachment/download URL when true; otherwise
                return a URL suitable for streaming inline.

        Returns:
            Absolute URL constructed from the configured API base URL and the
            token returned by the server.
        """
        try:
            response: Response = StoreLimitless.request(self.user, "POST", f"/auth/file/{self.id}/public-link")
            data: Any = response.json()
            token: Any = data.get("public_token") if isinstance(data, dict) else None
            if not isinstance(token, str) or not token:
                raise TypeError("public_token must be a non-empty string")
        except (ValueError, KeyError, TypeError) as error:
            if isinstance(error, StoreLimitlessResponseError):
                raise
            raise StoreLimitlessResponseError("StoreLimitless server returned an invalid public link response") from error

        route: str = "download" if download else "stream"
        return f"{StoreLimitless.API_URL}/public/{route}/{token}"

    def private_stream_link(self) -> tuple[str, int]:
        """Create a stream URL bound to the current login session.

        Returns:
            A pair containing the absolute stream URL and the server-reported
            expiration in seconds. The backend currently advertises 24 hours,
            but logout revokes the link earlier.

        Security:
            Treat this URL as a bearer secret. Session-bound does not mean safe
            to publish in logs, source control, or public issue reports.
        """
        return self._session_link("private-stream-link", "stream")

    def private_download_link(self) -> tuple[str, int]:
        """Create a download URL bound to the current login session.

        Use this for trusted temporary workflows. For a deliberately shareable
        link that remains independent of the login session, use
        public_link(download=True) instead.

        Returns:
            A pair containing the absolute download URL and server-reported
            expiration in seconds.
        """
        return self._session_link("download-link", "download")

    def _session_link(self, endpoint: str, route: str) -> tuple[str, int]:
        """Create and validate a session-bound file link.

        The public methods choose the exact endpoint and inline/download route;
        this helper validates the returned token and expiry and builds the
        absolute URL. The server remains authoritative for expiry and revocation.
        """
        response = StoreLimitless.request(self.user, "POST", f"/auth/file/{self.id}/{endpoint}")
        try:
            data: Any = response.json()
            token: Any = data.get("public_token") if isinstance(data, dict) else None
            expires_in: Any = data.get("expires_in") if isinstance(data, dict) else None
            if not isinstance(token, str) or not token:
                raise TypeError("public_token must be a non-empty string")
            if isinstance(expires_in, bool) or not isinstance(expires_in, int) or expires_in <= 0:
                raise TypeError("expires_in must be a positive integer")
            return f"{StoreLimitless.API_URL}/public/{route}/{token}", expires_in
        except (ValueError, TypeError) as error:
            raise StoreLimitlessResponseError(
                "StoreLimitless server returned an invalid session-bound link response"
            ) from error

    def metadata(self) -> dict[str, Any]:
        """Fetch current server-side metadata for this file by its ID.

        Returns:
            A dictionary containing the server-reported file ID, name, type and
            size. This method does not mutate the existing File snapshot.
        """
        response = StoreLimitless.request(self.user, "GET", f"/auth/file/{self.id}/metadata")
        try:
            data: Any = response.json()
            if not isinstance(data, dict):
                raise TypeError("expected a JSON object")
            if data.get("id") != self.id or not isinstance(data.get("name"), str):
                raise TypeError("missing or invalid file identity")
            if not isinstance(data.get("type"), str) or isinstance(data.get("size"), bool) or not isinstance(data.get("size"), int):
                raise TypeError("missing or invalid file fields")
            return data
        except (ValueError, TypeError) as error:
            raise StoreLimitlessResponseError("StoreLimitless server returned invalid file metadata") from error

    def download(self, path: str | Path | None = None, *, quiet: bool = False) -> Path:
        """Download this file to a local path with bounded-memory streaming.

        Args:
            path: Destination path. When omitted, use the file's remote name in
                the current working directory. Parent directories are created.
            quiet: Suppress the terminal progress bar when true.

        Returns:
            pathlib.Path to the completed destination file.

        Raises:
            StoreLimitlessConnectionError: The network transfer cannot complete.
            StoreLimitlessResponseError: The server returns invalid content-size
                metadata.
        """
        output_path: Path = Path(path) if path is not None else Path(self.name)
        response: Response = StoreLimitless.request(
            self.user,
            "GET",
            self.public_link(download=True).removeprefix(StoreLimitless.API_URL),
            stream=True,
            timeout=(15, 120),
        )

        try:
            total: int = int(response.headers.get("Content-Length", self.size))
            if total < 0:
                raise ValueError("content length cannot be negative")
        except (ValueError, TypeError) as error:
            raise StoreLimitlessResponseError("StoreLimitless server returned an invalid content length") from error

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with response:
            with tqdm(
                total=total,
                unit="B",
                unit_scale=True,
                unit_divisor=1024,
                desc=self.name,
                disable=quiet,
                ncols=get_terminal_size(fallback=(80, 24)).columns,
                bar_format="{desc} {bar} {n_fmt}/{total_fmt} {rate_fmt} ETA {remaining}",
            ) as bar:
                with output_path.open("wb") as output:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            bar.update(output.write(chunk))
        return output_path

    def stream(self, *, application: str = "vlc", private: bool = False) -> None:
        """Open this file in a media player and wait for the player to exit.

        Args:
            application: Player executable name or path. Defaults to VLC; when
                VLC is missing, the SDK attempts supported platform installation.
            private: Use a session-bound stream URL when true, so logout revokes
                the generated link. The default uses a public stream URL.
        """
        executable: str = ensure_vlc() if application.lower() == "vlc" else application
        url: str = self.private_stream_link()[0] if private else self.public_link()
        process: Popen[bytes] = Popen([executable, url])
        process.wait()

    def rename(self, name: str) -> File:
        """Rename this file remotely and update the local name snapshot.

        Args:
            name: New leaf filename, excluding a parent directory path.

        Raises:
            ValueError: The name is empty, dot, dot-dot, or contains a slash.
            StoreLimitlessResponseError: The server response is malformed.
        """
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise ValueError("Invalid file name")
        response: Response = StoreLimitless.request(self.user, "PATCH", f"/auth/file/{self.id}/rename", params={"name": name})
        try:
            data: Any = response.json()
            if isinstance(data, dict) and isinstance(data.get("name"), str):
                self.name = data["name"]
            else:
                raise TypeError("missing updated name")
        except (ValueError, TypeError) as error:
            raise StoreLimitlessResponseError("StoreLimitless server returned an invalid file rename response") from error
        return self

    def move(self, destination: Directory | str | Path) -> File:
        """Move this file to an existing remote directory.

        Args:
            destination: Directory object or remote directory path. When a
                Directory is provided, ownership is checked locally.

        Returns:
            This File object with its parent directory updated.

        Raises:
            ValueError: The destination belongs to another account.
            StoreLimitlessResponseError: The server returns malformed file metadata.
        """
        from .directory import Directory

        if isinstance(destination, Directory):
            if destination.user.username != self.user.username:
                raise ValueError("Destination directory belongs to another user")
            directory_path: str = str(destination.path)
        else:
            directory_path = str(destination)

        response: Response = StoreLimitless.request(
            self.user, "PATCH", f"/auth/file/{self.id}/move", params={"directory": directory_path}
        )
        try:
            data: Any = response.json()
            raw_id: Any = data.get("directory_id") if isinstance(data, dict) else None
            raw_path: Any = data.get("directory") if isinstance(data, dict) else None
            if isinstance(raw_id, bool) or not isinstance(raw_id, int) or not isinstance(raw_path, str):
                raise TypeError("missing or invalid destination directory fields")
            parent = destination if isinstance(destination, Directory) else None
            self.directory = parent if parent is not None else Directory(self.user, raw_id, Path(raw_path), None, None)
        except (ValueError, TypeError) as error:
            raise StoreLimitlessResponseError("StoreLimitless server returned an invalid file move response") from error
        return self

    def rm(self) -> File:
        """Move this file to the trash and update this snapshot."""
        StoreLimitless.request(self.user, "DELETE", f"/auth/file/{self.id}")
        self.deleted_at = datetime.now(timezone.utc)
        return self

    def delete(self) -> None:
        """Permanently delete this file from the trash.

        The method does not return a File because the remote record is removed.
        """
        StoreLimitless.request(self.user, "DELETE", f"/auth/trash/file/{self.id}")

    def restore(self) -> File:
        """Restore this file from the trash and update this snapshot."""
        StoreLimitless.request(self.user, "POST", f"/auth/trash/file/{self.id}/restore")
        self.deleted_at = None
        return self
