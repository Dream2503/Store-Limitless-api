"""Server lifecycle, authentication, and HTTP transport for StoreLimitless."""

from __future__ import annotations

from atexit import register
from pathlib import Path
from signal import SIGINT, SIGTERM, SIG_DFL, raise_signal, signal
from subprocess import PIPE, Popen, TimeoutExpired
from time import sleep
from typing import TYPE_CHECKING, Any, cast

from requests import (
    ConnectionError as RequestsConnectionError,
    RequestException,
    Response,
    Timeout as RequestsTimeout,
    get,
    request as requests_request,
)

from .exception import StoreLimitlessConnectionError, StoreLimitlessError, StoreLimitlessHTTPError, StoreLimitlessResponseError

if TYPE_CHECKING:
    from .user import User


class StoreLimitless:
    """Namespace for SDK operations; instances are intentionally not supported.

    Call :meth:`initialize` to configure the API URL and optionally launch the bundled local server, then use :meth:`register` and :meth:`login`.
    """

    API_URL: str = "http://127.0.0.1:8000"
    _process: Popen[str] | None = None

    def __new__(cls, *args: Any, **kwargs: Any) -> StoreLimitless:
        """Prevent constructing an instance of this class-level client."""

        raise TypeError("StoreLimitless cannot be instantiated")

    @classmethod
    def close(cls) -> None:
        """Stop the server process launched by :meth:`initialize`, if any."""

        process: Popen[str] | None = cls._process
        cls._process = None

        if process is None or process.poll() is not None:
            return

        process.terminate()

        try:
            process.wait(timeout=5)

        except TimeoutExpired:
            process.kill()
            process.wait()

    @classmethod
    def signal_handler(cls, signum: int, _frame: Any) -> None:
        """Close the managed server before restoring and re-raising a signal."""

        cls.close()
        signal(signum, SIG_DFL)
        raise_signal(signum)

    @classmethod
    def _is_server_running(cls) -> bool:
        """Check the public readiness endpoint without launching a process."""

        try:
            response: Response = get(f"{cls.API_URL}/public/ready", timeout=3)
            return response.ok

        except RequestException:
            return False

    @classmethod
    def request(cls, user: User | None, method: str, path: str, **kwargs: Any) -> Response:
        """Send an HTTP request and normalize connection and HTTP errors.

        The request receives a bearer token when ``user`` is supplied. If the server rejects an expired/invalid token, the client logs in once using
        the credentials held by that ``User`` and retries the original request. Arbitrary ``requests.request`` keyword arguments (``params``,
        ``json``, ``headers``, ``data``, ``stream``, and ``timeout``, for example) are passed through.
        """
        retry_auth: bool = bool(kwargs.pop("_retry_auth", True))
        custom_headers: dict[str, str] = dict(kwargs.pop("headers", {}))
        headers: dict[str, str] = {
            **({"Authorization": f"Bearer {user.token}"} if user else {}),
            **custom_headers,
        }

        try:
            response: Response = requests_request(method, f"{cls.API_URL}{path}", headers=headers, **kwargs)

        except RequestsConnectionError as error:
            raise StoreLimitlessConnectionError(f"Could not connect to StoreLimitless server at {cls.API_URL}") from error

        except RequestsTimeout as error:
            raise StoreLimitlessConnectionError("Request to StoreLimitless server timed out") from error

        except RequestException as error:
            raise StoreLimitlessError(f"Request to StoreLimitless server failed: {error}") from error

        if response.ok:
            return response

        try:
            response_data: Any = response.json()
            message: str = str(response_data.get("detail", response_data.get("message", response.text))) \
                if isinstance(response_data, dict) \
                else response.text

        except (ValueError, AttributeError):
            message = response.text

        if response.status_code == 401 and user is not None and retry_auth and message.strip().lower() == "invalid token":
            refreshed: User = cls.login(user.username, user.password)
            user.__dict__.update(refreshed.__dict__)
            return cls.request(user, method, path, _retry_auth=False, headers=custom_headers, **kwargs)

        raise StoreLimitlessHTTPError(response.status_code, message or f"HTTP {response.status_code}")

    @classmethod
    def initialize(cls, *, server_path: str | Path | None = None, server_ip: str = "http://127.0.0.1:8000") -> None:
        """Configure the API URL and start the bundled server if necessary.

        ``server_path`` overrides the platform-specific bundled executable. If the configured URL already responds at ``/public/ready``, no local
        process is started.
        """
        from platform import system

        cls.API_URL = server_ip.rstrip("/")

        if cls._is_server_running():
            return

        if server_path is None:
            match system():
                case "Linux":
                    server_path = Path(__file__).parent / "bin" / "linux" / "StoreLimitlessServer_0.1.0"

                case "Windows":
                    server_path = Path(__file__).parent / "bin" / "windows" / "StoreLimitlessServer_0.1.0.exe"

                case operating_system:
                    raise RuntimeError(f"Bundled StoreLimitless server is not available for {operating_system}")

        executable: Path = Path(cast(Path, server_path)).expanduser()

        if not executable.is_file():
            raise FileNotFoundError(f"Server executable not found: {executable}")

        if system() != "Windows":
            executable.chmod(executable.stat().st_mode | 0o111)

        cls.close()
        cls._process = Popen([str(executable)], stdin=PIPE, text=True)

        if cls._process.stdin is None:
            cls.close()
            raise StoreLimitlessConnectionError("Could not open the server process command pipe")

        cls._process.stdin.write("server\n")
        cls._process.stdin.flush()

        try:
            for _ in range(300):
                process: Popen[str] | None = cls._process

                if process is None or process.poll() is not None:
                    cls._process = None
                    raise StoreLimitlessConnectionError("StoreLimitless server stopped before becoming ready")

                try:
                    response: Response = get(f"{cls.API_URL}/public/ready", timeout=0.25)

                    if response.ok and response.json().get("ready") is True:
                        return

                except (RequestException, ValueError, AttributeError, TypeError):
                    pass

                sleep(0.1)

            raise StoreLimitlessConnectionError(f"StoreLimitless server failed to become ready at {cls.API_URL}")

        except BaseException:
            cls.close()
            raise

    @classmethod
    def login(cls, username: str, password: str) -> User:
        """Authenticate and return a :class:`User` SDK object."""

        from .user import User

        if cls._process is None and not cls._is_server_running():
            raise StoreLimitlessConnectionError("StoreLimitless server is not running. Call StoreLimitless.initialize() or start the server.")

        response: Response = cls.request(None, "POST", "/auth/login", params={"username": username, "password": password})

        try:
            reply, user_data, home_data = response.json()
            return User(reply["access_token"], **user_data, password=password, home=home_data)

        except (ValueError, KeyError, TypeError) as error:
            raise StoreLimitlessResponseError("StoreLimitless server returned an invalid login response") from error

    @classmethod
    def register(cls, first_name: str, last_name: str, username: str, password: str) -> None:
        """Register a new account. Raises :class:`StoreLimitlessHTTPError` on failure."""

        if cls._process is None and not cls._is_server_running():
            raise StoreLimitlessConnectionError("StoreLimitless server is not running. Call StoreLimitless.initialize() or start the server.")

        cls.request(
            None,
            "POST",
            "/auth/register",
            json={
                "first_name": first_name,
                "last_name": last_name,
                "username": username,
                "password": password,
            },
        )


register(StoreLimitless.close)
signal(SIGINT, StoreLimitless.signal_handler)
signal(SIGTERM, StoreLimitless.signal_handler)
