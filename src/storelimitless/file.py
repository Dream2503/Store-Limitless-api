"""File models, public links, downloads, playback, and file lifecycle operations."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from shutil import get_terminal_size
from subprocess import Popen
from typing import TYPE_CHECKING, Any

from requests import Response
from tqdm import tqdm

from .auth import StoreLimitless
from .exception import StoreLimitlessResponseError
from .utils import parse_datetime, ensure_vlc

if TYPE_CHECKING:
    from .directory import Directory
    from .user import User


class File:
    """Represent a stored file and expose operations for that file.

    ``directory`` supplies both the file's containing path and its authenticated ``User`` context. Search and trash results may use a lightweight
    directory reference where the backend did not return parent timestamps.
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
        self.directory: Directory = directory
        self.id: int = id
        self.name: str = name
        self.type: str = type
        self.size: int = size
        self.modified_at: datetime = modified_at
        self.deleted_at: datetime | None = deleted_at
        self.data_center: str = data_center

    @classmethod
    def from_payload(cls, directory: Directory, data: Any) -> File:
        """Create a file object from the JSON representation used by the API."""

        if not isinstance(data, dict):
            raise StoreLimitlessResponseError("StoreLimitless server returned an invalid file object")

        try:
            file_id: Any = data["id"]
            size: Any = data["size"]
            name: Any = data["name"]
            file_type: Any = data.get("type") or "application/octet-stream"
            data_center: Any = data["data_center"]
            modified_at: datetime | None = parse_datetime(data.get("modified_at"))
            deleted_at: datetime | None = parse_datetime(data.get("deleted_at"))

            if isinstance(file_id, bool) or not isinstance(file_id, int):
                raise TypeError("id must be an integer")

            if isinstance(size, bool) or not isinstance(size, int):
                raise TypeError("size must be an integer")

            if not isinstance(name, str) or not isinstance(file_type, str) or not isinstance(data_center, str):
                raise TypeError("invalid string field")

            if modified_at is None:
                raise TypeError("modified_at is missing")

            return cls(directory, file_id, name, file_type, size, modified_at, deleted_at, data_center)

        except (KeyError, TypeError) as error:
            raise StoreLimitlessResponseError("StoreLimitless server returned an invalid file object") from error

    def __repr__(self) -> str:
        """Return a developer-oriented representation of this file."""

        return (
            f"File(directory={self.directory!r}, id={self.id}, name={self.name!r}, "
            f"type={self.type!r}, size={self.size}, modified_at={self.modified_at!r}, "
            f"deleted_at={self.deleted_at!r}, data_center={self.data_center!r})"
        )

    def __str__(self) -> str:
        """Return a human-readable file summary."""

        size: int | float = float(self.size)
        unit: str = "B"
        units: tuple[str, ...] = ("B", "KB", "MB", "GB", "TB")

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
        """Return the user account associated with this file."""

        return self.directory.user

    def public_link(self, *, download: bool = False) -> str:
        """Create and return a public stream or download URL for this file.

        The returned URL is a bearer link: anyone who obtains it may be able to access the file while the token remains valid. Share it accordingly.
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
        """Create a session-bound stream URL for this file.

        Unlike :meth:`public_link`, the token returned by this route is bound
        to the current login session. The server stops accepting it after logout.
        Use this when a media player or application needs a URL but access should
        not outlive the current login session.

        Returns:
            A tuple containing the absolute stream URL and lifetime in seconds.
            The current backend reports 86400 seconds; logout can revoke access
            earlier.

        Security:
            Treat the URL as a bearer secret. Session-bound does not mean safe
            to paste into logs, public issues, or source control.
        """
        return self._session_link("private-stream-link", "stream")

    def private_download_link(self) -> tuple[str, int]:
        """Create a session-bound download URL for this file.

        The token is bound to the current account login session and is revoked
        when that session is logged out. Use this method for short-lived sharing
        with a trusted local workflow. For a deliberately shareable public link,
        use :meth:`public_link(download=True)` instead.

        Returns:
            The absolute download URL and the server-reported expiration in
            seconds. The current backend reports 86400 seconds unless logout
            revokes it sooner.
        """
        return self._session_link("download-link", "download")

    def _session_link(self, endpoint: str, route: str) -> tuple[str, int]:
        """Create a session-bound token and validate the response envelope.

        Kept private so public methods determine both the precise API route and
        whether the resulting URL streams inline or is downloaded as an
        attachment. The backend is authoritative for token expiry and revocation.
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
            raise StoreLimitlessResponseError("StoreLimitless server returned an invalid session-bound link response") from error

    def metadata(self) -> dict[str, Any]:
        """Fetch the current server-side metadata for this file by ID.

        The result contains the server's file ID, name, media type and size.
        It is useful when the caller has a File object but needs a fresh
        metadata snapshot after an upload, rename or other independent operation.
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
        """Download this file to a local path and display byte progress.

        The source is fetched from the backend's signed download route and
        streamed in bounded chunks rather than loaded wholly into memory. If
        path is omitted, the file is written under its current remote name in
        the process's working directory. Parent directories are created.

        Args:
            path: Destination file path or None to use the remote file name.
            quiet: Disable the terminal progress bar when true.

        Returns:
            A pathlib.Path pointing to the destination file.

        Raises:
            StoreLimitlessHTTPError: The server rejects access to this file.
            StoreLimitlessConnectionError: The network transfer fails.
            StoreLimitlessResponseError: Content-Length metadata is invalid.
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

        with tqdm(
                total=total,
                unit="B",
                unit_scale=True,
                unit_divisor=1024,
                desc=self.name,
                disable=quiet,
                ascii=" ━",
                colour="green",
                ncols=get_terminal_size(fallback=(80, 24)).columns,
                bar_format="\033[92m{desc}\033[0m{bar}\033[0m \033[92m{n_fmt}/{total_fmt}\033[0m \033[91m{rate_fmt}\033[0m eta \033[96m{remaining}\033[0m",
        ) as bar:
            with output_path.open("wb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        bar.update(output.write(chunk))

        return output_path

    def stream(self, *, application: str = "vlc") -> None:
        """Open this file's stream URL in ``application`` and wait for it to exit.

        The default ``vlc`` application is located automatically and may be installed by :func:`ensure_vlc` on supported platforms. Pass another
        executable name or path to use a different player.
        """

        executable: str = ensure_vlc() if application.lower() == "vlc" else application
        process: Popen[bytes] = Popen([executable, self.public_link()])
        process.wait()

    def rename(self, name: str) -> File:
        """Rename this file and update the current object's ``name`` field."""

        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise ValueError("Invalid file name")

        response: Response = StoreLimitless.request(self.user, "PATCH", f"/auth/file/{self.id}/rename", params={"name": name})

        try:
            data: Any = response.json()

            if isinstance(data, dict) and isinstance(data.get("name"), str):
                self.name = data["name"]

            else:
                self.name = name

            return self

        except (ValueError, TypeError) as error:
            raise StoreLimitlessResponseError("StoreLimitless server returned an invalid file rename response") from error

    def move(self, destination: Directory | str | Path) -> File:
        """Move this file into an existing directory."""

        from .directory import Directory

        if isinstance(destination, Directory):
            if destination.user.username != self.user.username:
                raise ValueError("Cannot move a file to another user's filesystem")

            destination_path: str = str(destination.path)

        else:
            destination_path = str(destination)

        response: Response = StoreLimitless.request(self.user, "PATCH", f"/auth/file/{self.id}/move", params={"directory": destination_path})

        try:
            data: Any = response.json()

            if isinstance(destination, Directory):
                self.directory = destination

            elif isinstance(data, dict):
                from .directory import Directory

                directory_id: Any = data.get("directory_id", self.directory.id)
                directory_path: Any = data.get("directory", destination_path)

                if isinstance(directory_id, int) and isinstance(directory_path, str):
                    self.directory = Directory(self.user, directory_id, Path(directory_path), None, None)

            return self

        except (ValueError, TypeError) as error:
            raise StoreLimitlessResponseError("StoreLimitless server returned an invalid file move response") from error

    def rm(self) -> File:
        """Move this file to the trash and return this object."""

        StoreLimitless.request(self.user, "DELETE", f"/auth/file/{self.id}")
        self.deleted_at = datetime.now(timezone.utc)
        return self

    def delete(self) -> None:
        """Permanently delete this file from the trash."""

        StoreLimitless.request(self.user, "DELETE", f"/auth/trash/file/{self.id}")

    def restore(self) -> File:
        """Restore this file from the trash."""

        StoreLimitless.request(self.user, "POST", f"/auth/trash/file/{self.id}/restore")
        self.deleted_at = None
        return self
