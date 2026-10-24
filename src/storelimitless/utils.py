"""Utility helpers for StoreLimitless SDK file and directory operations.

This module provides API-payload validation helpers for directory metadata and optional timestamps, along with VLC discovery and installation support
for media streaming. VLC installation is attempted automatically on supported Windows and Debian/Ubuntu Linux environments when no executable is
found.
"""

from __future__ import annotations

from datetime import datetime
from os import name, environ
from pathlib import Path
from platform import system
from shutil import which
from subprocess import run, CalledProcessError
from typing import Any, TYPE_CHECKING

from .exception import StoreLimitlessResponseError

if TYPE_CHECKING:
    from .directory import Directory
    from .user import User


def parse_datetime(value: Any) -> datetime | None:
    """Parse an optional ISO timestamp returned by the API."""

    if not value:
        return None

    if not isinstance(value, str):
        raise StoreLimitlessResponseError("StoreLimitless server returned an invalid timestamp")

    try:
        return datetime.fromisoformat(value)

    except ValueError as error:
        raise StoreLimitlessResponseError("StoreLimitless server returned an invalid timestamp") from error


def object_id(value: Any, *, field: str) -> int:
    """Validate an integer identifier from an API response."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise StoreLimitlessResponseError(f"StoreLimitless server returned an invalid {field}")

    return value


def directory_from_payload(user: User, data: Any) -> Directory:
    """Construct a directory from the backend's JSON object."""

    from .directory import Directory

    if not isinstance(data, dict):
        raise StoreLimitlessResponseError("StoreLimitless server returned an invalid directory object")

    try:
        raw_path: Any = data["path"]

        if not isinstance(raw_path, str):
            raise TypeError("path must be a string")

        return Directory(
            user,
            object_id(data["id"], field="directory ID"),
            Path(raw_path),
            parse_datetime(data.get("modified_at")),
            parse_datetime(data.get("deleted_at")),
        )

    except (KeyError, TypeError) as error:
        raise StoreLimitlessResponseError("StoreLimitless server returned an invalid directory object") from error


def ensure_vlc(urllib_request: Any = urllib_request) -> str:
    """Find VLC and attempt a platform-specific installation if it is missing.

    The SDK's existing behavior installs VLC automatically on supported Windows and Debian/Ubuntu Linux systems so that :meth:`File.stream` can be
    used without a separate player setup step.
    """
    vlc: str | None = which("vlc")

    if vlc:
        return vlc

    if name == "nt":
        program_files: str | None = environ.get("ProgramFiles")
        program_files_x86: str | None = environ.get("ProgramFiles(x86)")
        candidates: list[Path | None] = [
            Path(program_files) / "VideoLAN/VLC/vlc.exe" if program_files else None,
            Path(program_files_x86) / "VideoLAN/VLC/vlc.exe" if program_files_x86 else None,
        ]

        for candidate in candidates:
            if candidate and candidate.is_file():
                return str(candidate)

        print("VLC is not installed. Downloading and installing VLC for you...")
        vlc_dir: Path = Path.home() / "AppData" / "Local" / "StoreLimitless" / "vlc"
        vlc_path: Path = vlc_dir / "vlc.exe"
        installer: Path = vlc_dir / "vlc-installer.exe"
        vlc_dir.mkdir(parents=True, exist_ok=True)
        urllib_request.urlretrieve("https://get.videolan.org/vlc/3.0.21/win64/vlc-3.0.21-win64.exe", installer)
        run([str(installer), "/S", f"/D={vlc_dir}"], check=True)
        installer.unlink(missing_ok=True)

        if vlc_path.is_file():
            return str(vlc_path)

        raise FileNotFoundError("VLC installation failed.")

    if system() == "Linux":
        print("VLC is not installed. Installing VLC for you...")

        try:
            run(["sudo", "apt", "update"], check=True)
            run(["sudo", "apt", "install", "-y", "vlc"], check=True)

        except CalledProcessError as error:
            raise RuntimeError(
                "VLC is required, but StoreLimitless could not install it automatically.\n\n"
                "Please install it manually by running:\n"
                "sudo apt update && sudo apt install vlc"
            ) from error

        vlc = which("vlc")

        if vlc:
            return vlc

    raise RuntimeError(f"VLC automatic installation is not supported on {system()}.")
