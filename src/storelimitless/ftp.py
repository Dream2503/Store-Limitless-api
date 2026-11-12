"""Optional FTP client for the StoreLimitless virtual filesystem.

The StoreLimitless application starts FTP separately from its HTTP API. The
FTP listener defaults to TCP port 2121 and exposes the authenticated user's
/home directory as FTP /. The current server uses plain FTP unless it is
configured separately for TLS; credentials and file contents are therefore
not encrypted by this client.
"""

from __future__ import annotations

from datetime import datetime
from ftplib import FTP, Error as FTPProtocolError
from io import BytesIO
from os import replace as atomic_replace
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING, Any, Callable, TypeVar

from .exception import StoreLimitlessConnectionError, StoreLimitlessFTPError

if TYPE_CHECKING:
    from .user import User

T = TypeVar("T")


class FTPClient:
    """Connected client for the StoreLimitless FTP virtual filesystem.

    This is a separate TCP service, not an HTTP endpoint. Supply the address
    of the machine running StoreLimitless itself, not the Vercel API proxy
    hostname. The default FTP port is 2121.

    Security:
        The app's default FTP listener is plain FTP. Do not use this client
        across an untrusted network unless the server is separately configured
        for an encrypted transport. This SDK wrapper does not enable TLS because
        the bundled StoreLimitless FTP server does not configure FTPS by default.
    """

    def __init__(
            self,
            host: str,
            username: str,
            password: str,
            *,
            port: int = 2121,
            timeout: float = 30.0,
            passive: bool = True,
    ) -> None:
        if not host or not host.strip():
            raise ValueError("FTP host cannot be empty")
        if not 1 <= port <= 65535:
            raise ValueError("FTP port must be between 1 and 65535")
        if timeout <= 0:
            raise ValueError("FTP timeout must be greater than zero")
        if not username:
            raise ValueError("FTP username cannot be empty")

        self.host = host.strip()
        self.port = port
        self.username = username
        self.passive = passive
        self._ftp = FTP()
        self._closed = False

        try:
            self._call(self._ftp.connect, self.host, self.port, timeout=timeout)
            self._call(self._ftp.login, username, password)
            self._call(self._ftp.set_pasv, passive)
        except Exception:
            self.close()
            raise

    @classmethod
    def from_user(
            cls,
            user: User,
            host: str,
            *,
            port: int = 2121,
            timeout: float = 30.0,
            passive: bool = True,
    ) -> FTPClient:
        """Connect using the same username/password supplied at SDK login.

        The host must be the actual StoreLimitless server address. Vercel's
        HTTPS API proxy does not forward raw FTP traffic.
        """
        if not getattr(user, "password", ""):
            raise ValueError("The User object does not contain a password for FTP authentication")
        return cls(
            host,
            user.username,
            user.password,
            port=port,
            timeout=timeout,
            passive=passive,
        )

    def _call(self, operation: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Translate ftplib protocol and transport failures into SDK exceptions."""
        if self._closed:
            raise StoreLimitlessConnectionError("FTP connection is closed")
        try:
            return operation(*args, **kwargs)
        except FTPProtocolError as error:
            raise StoreLimitlessFTPError(f"StoreLimitless FTP command failed: {error}") from error
        except (OSError, EOFError, TimeoutError) as error:
            raise StoreLimitlessConnectionError(
                f"Could not communicate with StoreLimitless FTP server at {self.host}:{self.port}"
            ) from error

    @staticmethod
    def _path(path: str | Path) -> str:
        """Validate a virtual FTP path and prevent command-line injection."""
        value = str(path)
        if not value or "\r" in value or "\n" in value or "\x00" in value:
            raise ValueError("FTP path must be a non-empty path without control characters")
        return value

    @property
    def welcome(self) -> str:
        """Return the FTP server welcome banner."""
        if self._closed:
            raise StoreLimitlessConnectionError("FTP connection is closed")
        return str(self._ftp.getwelcome())

    def pwd(self) -> str:
        """Return the current virtual working directory."""
        return str(self._call(self._ftp.pwd))

    def cwd(self, path: str | Path) -> str:
        """Change the virtual working directory and return the resulting path."""
        self._call(self._ftp.cwd, self._path(path))
        return self.pwd()

    def listdir(self, path: str | Path | None = None) -> list[str]:
        """List immediate names in the current directory or the supplied path."""
        if path is None:
            result = self._call(self._ftp.nlst)
        else:
            result = self._call(self._ftp.nlst, self._path(path))
        return [str(value) for value in result]

    def listdir_details(self, path: str | Path | None = None) -> list[tuple[str, dict[str, str]]]:
        """List names and server-reported facts using the FTP MLSD command.

        The returned facts commonly include type, size, and modify time. The
        server remains authoritative for which facts it provides.
        """
        arguments = () if path is None else (self._path(path),)
        rows = self._call(lambda: list(self._ftp.mlsd(*arguments)))
        return [
            (str(name), {str(key): str(value) for key, value in facts.items()})
            for name, facts in rows
        ]

    def mkdir(self, path: str | Path) -> str:
        """Create a remote directory."""
        return str(self._call(self._ftp.mkd, self._path(path)))

    def rename(self, source: str | Path, destination: str | Path) -> str:
        """Rename or move a remote file or directory to another virtual path."""
        return str(self._call(self._ftp.rename, self._path(source), self._path(destination)))

    def delete(self, path: str | Path) -> str:
        """Move a remote file to the StoreLimitless trash.

        The server's FTP filesystem implements DELE as a soft delete; it does
        not permanently erase the stored file record.
        """
        return str(self._call(self._ftp.delete, self._path(path)))

    def rmdir(self, path: str | Path) -> str:
        """Move a remote directory to the StoreLimitless trash."""
        return str(self._call(self._ftp.rmd, self._path(path)))

    def size(self, path: str | Path) -> int | None:
        """Return the remote file size in bytes, or None when the server omits it."""
        result = self._call(self._ftp.size, self._path(path))
        return int(result) if result is not None else None

    def modified_at(self, path: str | Path) -> datetime:
        """Return the remote file/directory modification time from the MDTM command."""
        reply = str(self._call(self._ftp.sendcmd, f"MDTM {self._path(path)}"))
        parts = reply.split()
        if len(parts) < 2 or not parts[1].isdigit() or len(parts[1]) < 14:
            raise StoreLimitlessFTPError(f"FTP server returned an invalid MDTM timestamp: {reply!r}")
        try:
            return datetime.strptime(parts[1][:14], "%Y%m%d%H%M%S")
        except ValueError as error:
            raise StoreLimitlessFTPError(f"FTP server returned an invalid MDTM timestamp: {reply!r}") from error

    def download_bytes(self, remote_path: str | Path, *, block_size: int = 1024 * 1024) -> bytes:
        """Download one remote file into memory.

        Use download() for large files so the complete payload is not held
        in memory.
        """
        if block_size <= 0:
            raise ValueError("block_size must be greater than zero")
        buffer = BytesIO()
        self._call(
            self._ftp.retrbinary,
            f"RETR {self._path(remote_path)}",
            buffer.write,
            blocksize=block_size,
        )
        return buffer.getvalue()

    def download(
            self,
            remote_path: str | Path,
            local_path: str | Path | None = None,
            *,
            block_size: int = 1024 * 1024,
    ) -> Path:
        """Stream a remote file to disk and atomically replace the destination."""
        if block_size <= 0:
            raise ValueError("block_size must be greater than zero")
        remote = self._path(remote_path)
        destination = Path(local_path) if local_path is not None else Path(remote.rsplit("/", 1)[-1])
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with NamedTemporaryFile(
                mode="wb",
                prefix=f".{destination.name}.",
                suffix=".part",
                dir=destination.parent,
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                self._call(
                    self._ftp.retrbinary,
                    f"RETR {remote}",
                    temporary.write,
                    blocksize=block_size,
                )
            atomic_replace(str(temporary_path), str(destination))
            return destination
        except Exception:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise

    def upload_bytes(
            self,
            content: bytes | bytearray | memoryview,
            remote_path: str | Path,
            *,
            block_size: int = 1024 * 1024,
    ) -> str:
        """Upload bytes to a new or replacement remote file."""
        if block_size <= 0:
            raise ValueError("block_size must be greater than zero")
        stream = BytesIO(bytes(content))
        result = self._call(
            self._ftp.storbinary,
            f"STOR {self._path(remote_path)}",
            stream,
            blocksize=block_size,
        )
        return str(result)

    def append_bytes(
            self,
            content: bytes | bytearray | memoryview,
            remote_path: str | Path,
            *,
            block_size: int = 1024 * 1024,
    ) -> str:
        """Append bytes using the FTP APPE command where the backend allows it.

        The current StoreLimitless FTP filesystem rejects APPE when the target
        already exists, so this follows the server's supported append behavior
        rather than replacing existing remote files.
        """
        if block_size <= 0:
            raise ValueError("block_size must be greater than zero")
        stream = BytesIO(bytes(content))
        result = self._call(
            self._ftp.storbinary,
            f"APPE {self._path(remote_path)}",
            stream,
            blocksize=block_size,
        )
        return str(result)

    def append(
            self,
            local_path: str | Path,
            remote_path: str | Path | None = None,
            *,
            block_size: int = 1024 * 1024,
    ) -> str:
        """Append a local file's bytes using the server's FTP APPE behavior."""
        if block_size <= 0:
            raise ValueError("block_size must be greater than zero")
        source = Path(local_path).expanduser()
        if not source.is_file():
            raise FileNotFoundError(f"Append source is not a file: {source}")
        destination = self._path(remote_path if remote_path is not None else source.name)
        with source.open("rb") as stream:
            result = self._call(
                self._ftp.storbinary,
                f"APPE {destination}",
                stream,
                blocksize=block_size,
            )
        return str(result)

    def upload(
            self,
            local_path: str | Path,
            remote_path: str | Path | None = None,
            *,
            block_size: int = 1024 * 1024,
    ) -> str:
        """Stream a local file to the StoreLimitless virtual filesystem."""
        if block_size <= 0:
            raise ValueError("block_size must be greater than zero")
        source = Path(local_path).expanduser()
        if not source.is_file():
            raise FileNotFoundError(f"Upload source is not a file: {source}")
        destination = self._path(remote_path if remote_path is not None else source.name)
        with source.open("rb") as stream:
            result = self._call(
                self._ftp.storbinary,
                f"STOR {destination}",
                stream,
                blocksize=block_size,
            )
        return str(result)

    def close(self) -> None:
        """Close the FTP session, tolerating an already-disconnected server."""
        if self._closed:
            return
        self._closed = True
        try:
            self._ftp.quit()
        except Exception:
            try:
                self._ftp.close()
            except Exception:
                pass

    def __enter__(self) -> FTPClient:
        """Return this connected client for use as a context manager."""
        if self._closed:
            raise StoreLimitlessConnectionError("FTP connection is closed")
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        """Close the FTP connection at the end of a context manager."""
        self.close()
