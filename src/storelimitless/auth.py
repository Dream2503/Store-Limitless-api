from __future__ import annotations

from atexit import register
from pathlib import Path
from signal import signal, SIGINT, SIGTERM, raise_signal, SIG_DFL
from subprocess import PIPE, Popen, TimeoutExpired
from time import sleep
from typing import TYPE_CHECKING, cast

from requests import Response, RequestException, get, request, Timeout, ConnectionError

from .exception import StoreLimitlessConnectionError, StoreLimitlessError, StoreLimitlessHTTPError, StoreLimitlessResponseError

if TYPE_CHECKING:
    from .user import User


class StoreLimitless:
    API_URL: str
    _process: Popen | None = None

    def __new__(cls, *args, **kwargs):
        raise TypeError("StoreLimitless cannot be instantiated")

    @classmethod
    def close(cls) -> None:
        process = cls._process
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
    def signal_handler(cls, signum: int, _) -> None:
        cls.close()
        signal(signum, SIG_DFL)
        raise_signal(signum)

    @classmethod
    def _is_server_running(cls) -> bool:
        try:
            response: Response = get(f"{cls.API_URL}/public/ready", timeout=10)
            return response.ok

        except RequestException:
            return False

    @staticmethod
    def request(user: User | None, method: str, path: str, **kwargs) -> Response:
        try:
            response: Response = request(
                method,
                f"{StoreLimitless.API_URL}{path}",
                headers={
                    **({"Authorization": f"Bearer {user.token}"} if user else {}),
                    **kwargs.pop("headers", {}),
                },
                **kwargs,
            )
        except ConnectionError as e:
            raise StoreLimitlessConnectionError(f"Could not connect to StoreLimitless server at {StoreLimitless.API_URL}") from e

        except Timeout as e:
            raise StoreLimitlessConnectionError("Request to StoreLimitless server timed out") from e

        except RequestException as e:
            raise StoreLimitlessError(f"Request to StoreLimitless server failed: {e}") from e

        if not response.ok:
            try:
                message: str = response.json().get("detail", response.text)

            except (ValueError, AttributeError):
                message = response.text

                if user is not None and message == "Invalid token":
                    user.__dict__.update(StoreLimitless.login(user.username, user.password).__dict__)
                    return StoreLimitless.request(user, method, **kwargs)

            raise StoreLimitlessHTTPError(response.status_code, message or f"HTTP {response.status_code}")

        return response

    @classmethod
    def initialize(cls, *, server_path: str | Path | None = None, server_ip: str = "http://127.0.0.1:8000") -> None:
        from platform import system

        cls.API_URL = server_ip

        if server_path is None:
            match system():
                case "Linux":
                    server_path = Path(__file__).parent / "bin" / "linux" / "StoreLimitlessServer_0.1.0"

                case "Windows":
                    server_path = Path(__file__).parent / "bin" / "windows" / "StoreLimitlessServer_0.1.0.exe"

                case os:
                    raise RuntimeError(f"Bundled StoreLimitless server is not available for {os}")

        server_path = Path(cast(Path, server_path)).expanduser()

        if not server_path.is_file():
            raise FileNotFoundError(f"Server executable not found: {server_path}")

        if system() != "Windows":
            server_path.chmod(server_path.stat().st_mode | 0o111)

        if cls._is_server_running():
            return

        cls.close()
        cls._process = Popen([str(server_path)], stdin=PIPE, text=True)
        cls._process.stdin.write("server\n")
        cls._process.stdin.flush()

        try:
            for _ in range(300):
                if cls._process.poll() is not None:
                    cls._process = None
                    raise StoreLimitlessConnectionError("StoreLimitless server stopped before becoming ready")

                try:
                    response: Response = get(f"{cls.API_URL}/public/ready", timeout=0.1)

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
        from .user import User

        if cls._process is None and not cls._is_server_running():
            raise StoreLimitlessConnectionError(
                "StoreLimitless server is not running. Call StoreLimitless.initialize(server_path) or start the StoreLimitless server."
            )

        response: Response = cls.request(
            None,
            "POST",
            "/auth/login",
            params={"username": username, "password": password},
        )

        try:
            reply, user, home = response.json()
            return User(reply["access_token"], **user, password=password, home=home)

        except (ValueError, KeyError, TypeError) as e:
            raise StoreLimitlessResponseError("StoreLimitless server returned an invalid login response") from e

    @classmethod
    def register(cls, first_name: str, last_name: str, username: str, password: str) -> None:
        if cls._process is None and not cls._is_server_running():
            raise StoreLimitlessConnectionError(
                "StoreLimitless server is not running. Call StoreLimitless.initialize(server_path) or start the StoreLimitless server."
            )

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
