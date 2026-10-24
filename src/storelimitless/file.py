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
        """Create a stream URL bound to the current login session.

        Returns:
            A pair of the absolute URL and server-reported expiry in seconds.
            The current backend sets the link lifetime to 24 hours, but it is
            invalidated sooner if the associated login session is logged out.

        Security:
            Treat the URL as a bearer secret. Do not publish it in logs, source
            control, public issue reports, or other places that strangers can read.
        """
        return self._session_link("private-stream-link", "stream")

    def private_download_link(self) -> tuple[str, int]:
        """Create an attachment URL bound to the current login session.

        Use this for trusted, temporary workflows. For a deliberately
        shareable link that is not revoked by logout, use public_link(download=True).

        Returns:
            The absolute URL and the expiry in seconds reported by the server.
        """
        return self._session_link("download-link", "download")

    def _session_link(self, endpoint: str, route: str) -> tuple[str, int]:
        """Create and validate a session-bound file link.

        This private helper keeps token validation and URL construction in one
        place while allowing the public methods to select the exact API endpoint
        and whether the final URL streams inline or downloads as an attachment.
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
        """Fetch the current server-side metadata for this file.

        Returns:
            A dictionary with the file ID, name, media type and size from the
            current API response. This does not mutate the local File snapshot.
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
            raise StoreLimitlessResponseError(
                "StoreLimitless server returned invalid file metadata"
            ) from error

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


