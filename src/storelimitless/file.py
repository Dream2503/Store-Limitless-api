from __future__ import annotations

import os
import platform
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from shutil import get_terminal_size
from subprocess import Popen
from typing import TYPE_CHECKING, Literal
from urllib import request

from requests import Response
from tqdm import tqdm

from .auth import StoreLimitless
from .exception import StoreLimitlessResponseError

if TYPE_CHECKING:
    from .directory import Directory


def ensure_vlc() -> str:
    vlc: str | None = shutil.which("vlc")

    if vlc:
        return vlc

    if os.name == "nt":
        program_files: str | None = os.environ.get("ProgramFiles")
        program_files_x86: str | None = os.environ.get("ProgramFiles(x86)")
        candidates: list[Path | None] = [
            Path(program_files) / "VideoLAN/VLC/vlc.exe" if program_files else None,
            Path(program_files_x86) / "VideoLAN/VLC/vlc.exe" if program_files_x86 else None
        ]

        for candidate in candidates:
            if candidate and candidate.is_file():
                return str(candidate)

        print("VLC is not installed. Downloading and installing VLC for you...")
        vlc_dir: Path = Path.home() / "AppData" / "Local" / "StoreLimitless" / "vlc"
        vlc_path: Path = vlc_dir / "vlc.exe"
        installer: Path = vlc_dir / "vlc-installer.exe"
        vlc_dir.mkdir(parents=True, exist_ok=True)
        request.urlretrieve("https://get.videolan.org/vlc/3.0.21/win64/vlc-3.0.21-win64.exe", installer)
        subprocess.run([str(installer), "/S", f"/D={vlc_dir}"], check=True)
        installer.unlink(missing_ok=True)

        if vlc_path.is_file():
            return str(vlc_path)

        raise FileNotFoundError("VLC installation failed.")

    if platform.system() == "Linux":
        print("VLC is not installed. Installing VLC for you...")

        try:
            subprocess.run(["sudo", "apt", "update"], check=True)
            subprocess.run(["sudo", "apt", "install", "-y", "vlc"], check=True)

        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                "VLC is required, but StoreLimitless could not install it automatically.\n\n"
                "Please install it manually by running:\n"
                "sudo apt update && sudo apt install vlc"
            ) from e

        vlc: str | None = shutil.which("vlc")

        if vlc:
            return vlc

    raise RuntimeError(f"VLC automatic installation is not supported on {platform.system()}.")


class File:
    def __init__(
            self,
            directory: Directory,
            id: int,
            name: str,
            type: str,
            size: int,
            modified_at: datetime,
            deleted_at: datetime | None,
            data_center: str
    ) -> None:
        self.directory: Directory = directory
        self.id: int = id
        self.name: str = name
        self.type: str = type
        self.size: int = size
        self.modified_at: datetime = modified_at
        self.deleted_at: datetime | None = deleted_at
        self.data_center: str = data_center

    def __repr__(self) -> str:
        return (
            f"File(directory={self.directory!r}, "
            f"id={self.id}, "
            f"name={self.name!r}, "
            f"type={self.type!r}, "
            f"size={self.size}, "
            f"modified_at={self.modified_at!r}, "
            f"deleted_at={self.deleted_at!r}, "
            f"data_center={self.data_center!r})"
        )

    def __str__(self) -> str:
        unit: str = ""
        size: float = float(self.size)
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

    def download(self, path: str | Path | None = None, *, quiet: bool = False) -> Path:
        path = Path(path) if path is not None else Path(self.name)

        try:
            token: str = StoreLimitless.request(self.directory.user, "POST", f"/auth/file/{self.id}/public-link").json()["public_token"]

        except (ValueError, KeyError, TypeError) as e:
            raise StoreLimitlessResponseError("StoreLimitless server returned an invalid public link response") from e

        response: Response = StoreLimitless.request(self.directory.user, "GET", f"/public/download/{token}", stream=True)

        try:
            total: int = int(response.headers.get("Content-Length", self.size))

        except (ValueError, TypeError) as e:
            raise StoreLimitlessResponseError("StoreLimitless server returned an invalid content length") from e

        path.parent.mkdir(parents=True, exist_ok=True)

        with tqdm(
                total=total,
                unit="B",
                unit_scale=True,
                unit_divisor=1024,
                desc=self.name,
                disable=quiet,
                ascii=" ━",
                colour="green",
                ncols=get_terminal_size().columns,
                bar_format="\033[92m{desc}\033[0m{bar}\033[0m \033[92m{n_fmt}/{total_fmt}\033[0m \033[91m{rate_fmt}\033[0m eta \033[96m{remaining}\033[0m",
        ) as bar:
            with path.open("wb") as file:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        bar.update(file.write(chunk))

        return path

    def stream(self, *, application: Literal["vlc"] = "vlc") -> None:
        try:
            token: str = StoreLimitless.request(self.directory.user, "POST", f"/auth/file/{self.id}/public-link").json()["public_token"]

        except (ValueError, KeyError, TypeError) as e:
            raise StoreLimitlessResponseError("StoreLimitless server returned an invalid public link response") from e

        if application == "vlc":
            application = ensure_vlc()

        process: Popen = Popen([application, f"{StoreLimitless.API_URL}/public/stream/{token}"])
        process.wait()

    def rm(self) -> File:
        StoreLimitless.request(self.directory.user, "DELETE", f"/auth/file/{self.id}")
        return self

    def delete(self) -> None:
        StoreLimitless.request(self.directory.user, "DELETE", f"/auth/trash/file/{self.id}")

    def restore(self) -> File:
        StoreLimitless.request(self.directory.user, "POST", f"/auth/trash/file/{self.id}/restore")
        return self
