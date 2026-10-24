"""StoreLimitless Python SDK: package entry point and user guide.

StoreLimitless is a client library for interacting with a StoreLimitless server. It exposes a small, object-oriented interface for account
authentication, virtual-directory navigation, file uploads and downloads, streaming links, search, rename/move operations, and the recycle-bin
workflow.

The main public objects are:

* :class:`StoreLimitless`: configure the server, register accounts, and log in.
* :class:`User`: the authenticated account and its home directory.
* :class:`Directory`: a directory in the user's *remote StoreLimitless filesystem*, not a directory on the computer running Python.
* :class:`File`: metadata and operations for a remote stored file.
* :class:`DirectoryResult`: a pair of directory and file collections.
* :class:`FileTree`: an account-wide snapshot that includes active and trashed objects.
* :class:`UploadTask` and :class:`UploadJob`: wrappers for cross-device task-tray state and lifecycle.
* :class:`TorrentPreview`: file metadata resolved from a magnet link before downloading payload bytes.

The SDK also supports streamed ZIP64 downloads, session-bound file links that can be revoked at logout,
file metadata refresh, torrent previews, and typed access to the server's expanded upload progress model.

The exceptions exported here provide a stable way to handle connection, HTTP, and invalid-response failures without importing internal modules.

Quick start
===========

The SDK is intended to be used through these exported objects. In most cases, there is no need to call raw HTTP endpoints directly.

.. code-block:: python
    from storelimitless import Directory, DirectoryResult, File, StoreLimitless, User

    # Use an already-running server. The URL is the server's base URL.
    StoreLimitless.initialize(server_ip="http://127.0.0.1:8000")
    user: User = StoreLimitless.login("your_username", "your_password")
    home: Directory = user.home

    # List the immediate children of the home directory.
    listing: DirectoryResult = home.ls
    directory: Directory

    for directory in listing.directories:
        print("Folder:", directory.path)

    file: File

    for file in listing.files:
        print("File:", file.name, file.size, "bytes")

    # Create a folder, upload a local file, then search for it.
    projects: Directory = home.mkdir("projects")
    uploaded: File = projects.upload("./report.pdf", data_center="Discord")
    print("Uploaded:", uploaded)
    matches: DirectoryResult = user.search("report")

    for file in matches.files:
        print(file.directory.path / file.name)

    # Stop a local server process only if this SDK started it.
    StoreLimitless.close()

Connecting to a StoreLimitless server
=====================================

Call :meth:`StoreLimitless.initialize` before logging in. Its ``server_ip`` argument is the base URL used for HTTP calls; it is not a URL to a
particular endpoint.

.. code-block:: python
    from storelimitless import StoreLimitless

    # A local server:
    StoreLimitless.initialize(server_ip="http://127.0.0.1:8000")

    # A server reachable over a network (use HTTPS for untrusted networks):
    # StoreLimitless.initialize(server_ip="https://storage.example.com")

The SDK first checks whether the configured server is responding. If it is, the SDK uses that server and does not need to launch a bundled executable.
If no server is responding, it attempts to start the platform-specific bundled server. You can pass ``server_path`` to use an explicit executable:

.. code-block:: python
    StoreLimitless.initialize(server_path="/absolute/path/to/StoreLimitlessServer", server_ip="http://127.0.0.1:8000")

The bundled executable must exist and be runnable. The package's bundled server layout is platform-specific, so deployments without the expected
binary should provide ``server_path`` or start the server separately.

``StoreLimitless.close()`` stops the local child process launched by this SDK. It does not shut down a server that was already running independently
or a remote server. The SDK also arranges normal process-exit and termination cleanup for the managed child process.

A common application pattern is to use ``try/finally`` for explicit cleanup:

.. code-block:: python
    from storelimitless import StoreLimitless, User

    StoreLimitless.initialize(server_ip="http://127.0.0.1:8000")

    try:
        user: User = StoreLimitless.login("your_username", "your_password")
        print(user.home)

    finally:
        StoreLimitless.close()

Account registration and authentication
=======================================

Register an account with the required profile fields. Registration does not return a ``User`` object; log in after registering to get an authenticated
session.

.. code-block:: python
    from storelimitless import StoreLimitless, User

    StoreLimitless.register(first_name="Ada", last_name="Lovelace", username="ada", password="a-long-unique-password")
    user: User = StoreLimitless.login("ada", "a-long-unique-password")
    print(user.username)
    print(user.first_name, user.last_name)

``StoreLimitless.login(username, password)`` returns a :class:`User`. The object contains an access token and keeps the supplied password in memory so
the SDK can attempt re-authentication if the server explicitly reports an invalid token. Do not log, print, or serialize the complete internal state
of a ``User`` object, and never store the password in source control.

Authenticated requests include the bearer access token automatically. The login endpoint used by the current server accepts the username and password
as query parameters. Use HTTPS when connecting across an untrusted network, and configure server and proxy logs carefully because URL query parameters
can be recorded in access logs.

The public object model
=======================

StoreLimitless
--------------

``StoreLimitless`` is a class-level API namespace. It is not instantiated.

* ``StoreLimitless.initialize(server_path=None, server_ip="http://127.0.0.1:8000")`` configures the base URL and checks/starts the server.
* ``StoreLimitless.register(first_name, last_name, username, password)`` creates an account.
* ``StoreLimitless.login(username, password)`` authenticates and returns a ``User``.
* ``StoreLimitless.close()`` shuts down the locally managed server process, if one was started by the SDK.
* ``StoreLimitless.request(user, method, path, **kwargs)`` is the underlying HTTP transport. Most application code should prefer the higher-level
  methods on ``User``, ``Directory``, and ``File``.

User
----

A ``User`` represents the authenticated account.

* ``user.username``, ``user.first_name``, and ``user.last_name`` provide account identity fields.
* ``user.home`` returns the account's home :class:`Directory` object. The remote home path is normally ``/home``.
* ``user.metadata`` requests account metadata and returns a dictionary. The current backend exposes ``storage_used_bytes``.
* ``user.storage_used_bytes`` is a convenience property for the stored byte count. The backend's current calculation includes active and trashed
  files.
* ``user.trash`` returns a ``DirectoryResult`` of trashed directories and files.
* ``user.search(query, trash=False)`` searches file and directory metadata across the account. Pass ``trash=True`` to search trashed items.
* ``user.move(items, destination)`` sends a batch of :class:`File` and/or :class:`Directory` objects to an existing destination directory. Inspect the
  returned per-item results; not every item is guaranteed to move.

Directory and remote paths
--------------------------

A ``Directory`` is a remote StoreLimitless directory. A Python ``Path`` passed to ``Directory.upload`` identifies a local input file; a ``Directory``
object or path string used in navigation and move operations identifies a remote path. Do not confuse these two namespaces.

``Directory`` exposes the following operations:

* ``directory.ls`` lists the immediate child folders and files.
* ``directory.cd(name)`` navigates to a single child, ``..`` for the parent, or ``~`` / ``/`` for the home directory. ``directory / name`` is
  shorthand for ``directory.cd(name)``.
* ``directory.mkdir(name)`` creates a child directory.
* ``directory.search(query, trash=False)`` performs account-wide server-side search. ``directory.find(query, trash=False)`` is a compatibility alias
  for that search; it is not limited to the current subtree.
* ``directory.upload(source, data_center="Discord", ...)`` uploads a local file or imports an HTTP(S) URL.
* ``directory.rename(name)`` renames the directory. The server also updates descendant paths.
* ``directory.move(destination)`` moves the directory into an existing destination. Moving a directory into itself or a descendant is rejected by the
  server.
* ``directory.rm()`` moves the directory to the trash.
* ``directory.restore()`` restores a trashed directory.
* ``directory.delete()`` permanently deletes a directory that is already in the trash.

Example directory navigation and creation:

.. code-block:: python
    from storelimitless import Directory, DirectoryResult, File, User

    home: Directory = user.home

    # List contents. DirectoryResult is also unpackable as two values.
    result: DirectoryResult = home.ls
    folders: list[Directory]
    files: list[File]
    folders, files = result

    # Navigate to a child by name or with the / operator.
    # This raises ValueError if the child does not exist.
    documents: Directory

    if any(folder.path.name == "Documents" for folder in folders):
        documents = home / "Documents"

    parent: Directory = documents.cd("..")
    home_again: Directory = documents.cd("~")

    # Create a folder under the current remote directory.
    projects: Directory = home.mkdir("projects")

DirectoryResult
---------------

Directory and search listings use :class:`DirectoryResult`, which separates objects by kind instead of merging them into one list:

* ``result.directories`` is a list of ``Directory`` objects.
* ``result.files`` is a list of ``File`` objects.
* ``result.directory`` is the first directory or ``None`` if the list is empty.
* ``result.file`` is the first file or ``None`` if the list is empty.
* Iteration yields two values in order, so ``directories, files = result`` is supported.

File
----

A ``File`` holds remote metadata such as its name, parent directory, size, media type, modification time, deletion time, and storage data center.

* ``file.download(path=None, quiet=False)`` downloads the file to a local path, displays a progress bar unless ``quiet=True``, and returns the local
  ``Path``.
* ``file.stream(application="vlc")`` creates a stream link and opens it with the requested player executable. With the default ``"vlc"``, the SDK
  checks for VLC and may attempt installation on supported platforms if it is not found. Pass another executable name/path to use a different player.
* ``file.public_link(download=False)`` creates a public stream URL. Use ``download=True`` to request a download URL instead. Treat these URLs like
  bearer secrets.
* ``file.rename(name)`` renames the remote file.
* ``file.move(destination)`` moves the file into an existing destination directory.
* ``file.rm()`` moves the file to the trash.
* ``file.restore()`` restores a trashed file.
* ``file.delete()`` permanently deletes a file already in the trash.

Creating folders, listing, and navigating
==========================================

.. code-block:: python
    from storelimitless import Directory, File

    home: Directory = user.home
    projects: Directory = home.mkdir("projects")
    reports: Directory = projects.mkdir("reports")
    current: Directory = home
    current = current.cd("projects")
    current = current / "reports"
    folder: Directory

    for folder in current.ls.directories:
        print("Directory:", folder.path)

    file: File
    for file in current.ls.files:
        print(file.name, file.size, file.modified_at)

The home directory is a StoreLimitless virtual directory. Paths such as ``/home/projects`` are remote server paths, not paths on the local computer.
The SDK's directory navigation accepts one child name at a time; use multiple ``cd`` calls to walk through multiple levels.

Uploading local files and importing URLs
========================================

Call ``Directory.upload`` on the remote destination directory. It accepts a local filesystem path or an HTTP(S) URL. The storage destination is
selected with ``data_center``; the current SDK supports ``"Discord"`` and ``"Telegram"``.

.. code-block:: python
    from pathlib import Path
    from storelimitless import Directory, File

    destination: Directory = user.home.mkdir("uploads")

    # Upload a local file. A Path is always treated as a local path.
    uploaded: File = destination.upload(Path("./archive.zip"))
    print(uploaded.name, uploaded.size)

    # Select a storage provider explicitly.
    uploaded_video: File = destination.upload("./video.mkv", data_center="Telegram", quiet=False)

    # Import a remote URL without first saving it in the local working folder.
    imported: File = destination.upload("https://example.com/releases/archive.zip", data_center="Discord")

For SDK versions that expose upload tuning options, the following options control chunk transmission and job polling:

.. code-block:: python
    from storelimitless import Directory, File

    uploaded: File = destination.upload(
        "./large-video.mkv",
        data_center="Telegram",
        chunk_size=10 * 1024 * 1024,  # 10 MiB per chunk
        chunk_retries=3,
        poll_interval=0.2,
        job_timeout=3600,
        quiet=False,
    )

``chunk_size`` is measured in bytes. ``chunk_retries`` controls retries for retryable transfer failures; ``poll_interval`` is the delay between upload
status checks; ``job_timeout`` is the maximum time to wait for the server-side job to finish (``None`` can be used for an unbounded wait if supported
by the installed SDK version); and ``quiet=True`` disables the terminal progress bar. Check the installed version's function signature if you are
using an older SDK release.

The local-file workflow is conceptually:

#. The SDK asks the server to create an upload job.
#. It sends the file in chunks, tracking offsets and the full file size.
#. It polls the job status until the server reports completion or failure.
#. It refreshes the destination listing and returns the resulting ``File``.

URL imports are submitted to the server as a link-import job, then polled in the same way. A server-side transfer can continue after the initial
request, so the final storage status must be checked rather than assuming that a successful job-creation response means the file is already stored.

Downloading and streaming
=========================

Download a file to a local path:

.. code-block:: python
    from pathlib import Path
    from storelimitless import File

    # Writes ./downloads/report.pdf and creates its parent directory if needed.
    saved_path: Path = uploaded.download("./downloads/report.pdf")
    print("Saved to:", saved_path.resolve())

    # Suppress the progress bar for scripts and background tasks.
    saved_path = uploaded.download("./downloads/archive.zip", quiet=True)

If ``path`` is omitted, the SDK uses the remote file's name in the current working directory. The method returns a ``pathlib.Path`` so that callers
can check the resulting location or pass it to other local-file APIs.

Create a signed public URL or open media in a player:

.. code-block:: python
    from storelimitless import File

    stream_url: str = uploaded.public_link()
    download_url: str = uploaded.public_link(download=True)
    print("Stream:", stream_url)
    print("Download:", download_url)

    # Open the stream in VLC (VLC may be installed automatically if missing).
    uploaded.stream()

    # Or use another installed player executable.
    # uploaded.stream(application="mpv")

Public links should be treated as secrets: anyone who obtains a valid link may be able to access the corresponding stream or download, depending on
the link and server configuration. Avoid publishing links to private files.

Searching files and directories
===============================

Use ``user.search`` to search the entire account, including nested folders. The results are separated into directories and files. Search can include
the trash when requested.

.. code-block:: python
    from storelimitless import Directory, DirectoryResult, File, User

    results: DirectoryResult = user.search("meeting notes")
    directory: Directory

    for directory in results.directories:
        print("Folder match:", directory.path)

    file: File

    for file in results.files:
        print("File match:", file.directory.path / file.name)

    # Search items currently in the trash.
    trashed_matches: DirectoryResult = user.search("old report", trash=True)

    # Compatibility alias; this also performs an account-wide search.
    other_matches: DirectoryResult = user.home.find("report")

Search is delegated to the server. Do not assume ``Directory.search`` or ``Directory.find`` restricts results to that directory's descendants.
Matching rules and authorization are controlled by the server implementation.

Account metadata and storage usage
==================================

.. code-block:: python
    metadata: dict[str, object] = user.metadata
    print(metadata)
    storage_used_bytes: int = user.storage_used_bytes
    print("Bytes used:", storage_used_bytes)

The current server reports ``storage_used_bytes``. The documented backend calculation includes files in active storage and files in the trash. Treat
this value as server-reported metadata, not a calculation over files that are present on the local machine.

Rename and move operations
==========================

Rename a file or directory using its object method:

.. code-block:: python
    from storelimitless import Directory, File

    file: File = user.home.ls.files[0]
    file.rename("renamed-report.pdf")
    archive: Directory = user.home.mkdir("archive")
    file.move(archive)

    folder: Directory = user.home.mkdir("old-project")
    folder.rename("project-archive")
    folder.move(archive)

The destination of a move must be an existing StoreLimitless directory. A ``Directory`` object is useful because it carries the destination path and
user identity. The SDK rejects cross-user moves when given a destination ``Directory`` belonging to a different user; the server remains authoritative
for permission and path validation. Name collisions or invalid operations can raise :class:`StoreLimitlessHTTPError`.

For a mixed batch of files and directories, use ``user.move``:

.. code-block:: python
    from storelimitless import Directory, DirectoryResult, File

    home: Directory = user.home
    destination: Directory = home.mkdir("archive")
    listing: DirectoryResult = home.ls
    items: list[File | Directory] = []

    if listing.files:
        items.append(listing.files[0])

    if listing.directories:
        items.append(listing.directories[0])

    if items:
        result: dict[str, object] = user.move(items=items, destination=destination)
        print("Moved:", result.get("moved", []))
        print("Unchanged:", result.get("unchanged", []))
        print("Failed:", result.get("failed", []))

Bulk move results can contain successes, unchanged items, and per-item failures. Inspect those result collections even when the HTTP request itself
succeeds. After a directory move or rename, fetch the relevant directory again when you need fresh child paths or metadata.

Trash, restore, and permanent deletion
======================================

The SDK separates moving an item to trash from permanently deleting it:

* ``item.rm()`` moves a file or directory to trash.
* ``item.restore()`` restores an item that is in the trash.
* ``item.delete()`` permanently deletes an item that is already in the trash.
* ``user.trash`` lists trashed directories and files.

.. code-block:: python
    from storelimitless import DirectoryResult, File

    # Soft-delete: the item should appear in user.trash.
    file: File
    file.rm()
    trash: DirectoryResult = user.trash
    trashed_file: File

    for trashed_file in trash.files:
        print(trashed_file.name, trashed_file.deleted_at)

    # Restore a known trashed item.
    file.restore()

    # Permanently delete only when you truly intend to remove it.
    # file.delete()

Permanently deleting an item is different from ``rm`` and may not be recoverable. Keep references to trashed objects only as long as needed, and
refresh listings after changing an item's state.

Error handling
==============

All SDK-specific exceptions derive from :class:`StoreLimitlessError`.

* :class:`StoreLimitlessConnectionError`: the service cannot be reached, the request times out, the managed server fails to start, or a job does not
  finish within the configured timeout.
* :class:`StoreLimitlessHTTPError`: the service returns a non-success HTTP status. Inspect ``error.status_code`` and the exception message.
* :class:`StoreLimitlessResponseError`: the service responds, but its JSON or required fields do not match the schema expected by the SDK.
* :class:`StoreLimitlessError`: a broader transport or SDK failure not covered by a more specific exception.

.. code-block:: python
    from storelimitless import (
        StoreLimitless,
        StoreLimitlessError,
        StoreLimitlessHTTPError,
        StoreLimitlessConnectionError,
        StoreLimitlessResponseError,
        User,
    )

    try:
        StoreLimitless.initialize(server_ip="http://127.0.0.1:8000")
        user: User = StoreLimitless.login("your_username", "your_password")
        user.home.upload("./report.pdf")

    except StoreLimitlessHTTPError as error:
        print("Server rejected the request:", error.status_code, str(error))

    except StoreLimitlessConnectionError as error:
        print("Cannot reach the StoreLimitless service:", error)

    except StoreLimitlessResponseError as error:
        print("Unexpected server response:", error)

    except StoreLimitlessError as error:
        print("StoreLimitless operation failed:", error)

    finally:
        StoreLimitless.close()

Typical HTTP errors include authentication failures, missing files or folders, invalid names/paths, and name conflicts. Exact status codes and
messages are controlled by the server version; handle exceptions rather than relying only on a particular error string.

Using the raw request helper
============================

``StoreLimitless.request`` is exposed for advanced integrations and for an endpoint that does not yet have a dedicated SDK method. It returns a
``requests.Response`` and automatically attaches the bearer token when a ``User`` is supplied.

.. code-block:: python
    from requests import Response
    from storelimitless import StoreLimitless, User

    # Example: call a supported authenticated endpoint directly.
    response: Response = StoreLimitless.request(user, "GET", "/auth/metadata")
    metadata: dict[str, object] = response.json()

Pass standard ``requests.request`` keyword arguments such as ``params``, ``json``, ``headers``, ``data``, and ``stream`` as needed. Prefer the public
object methods when available because they validate responses and convert transport/API errors into the SDK's exception hierarchy.

Endpoint overview
=================

The current SDK uses the following server routes. These are included to help contributors understand which remote operation a client method calls;
callers should usually use the higher-level SDK methods described above.

* ``POST /auth/register``: register an account.
* ``POST /auth/login``: authenticate and obtain a token/profile/home payload.
* ``GET /auth/metadata``: read account metadata.
* ``GET /auth/directory``: list folders and files in a directory.
* ``POST /auth/create-folder``: create a directory.
* ``GET /auth/search``: search active or trashed objects.
* ``GET /auth/trash``: list trashed objects.
* ``POST /auth/upload``: create a local-file upload job.
* ``POST /auth/upload-link``: create a URL-import upload job.
* ``PUT /public/upload/{job_id}/chunk``: transfer a chunk for an upload job.
* ``GET /public/upload/{job_id}/status``: poll upload-job progress/status.
* ``POST /auth/file/{fid}/public-link``: create a public stream/download token.
* ``GET /public/stream/{token}`` and ``GET /public/download/{token}``: stream or download a file through a signed token.
* ``PATCH /auth/file/{fid}/rename`` and ``PATCH /auth/directory/{did}/rename``: rename files/directories.
* ``PATCH /auth/file/{fid}/move`` and ``PATCH /auth/directory/{did}/move``: move one file/directory.
* ``PATCH /auth/move``: bulk move files and directories.
* ``DELETE /auth/file/{fid}`` and ``DELETE /auth/directory/{did}``: move items to trash.
* ``POST /auth/trash/file/{fid}/restore`` and ``POST /auth/trash/directory/{did}/restore``: restore items.
* ``DELETE /auth/trash/file/{fid}`` and ``DELETE /auth/trash/directory/{did}``: permanently delete trashed items.

The backend is the source of truth for accepted parameters, response schemas, permission checks, and any limits. Refer to the matching server
version's route definitions when adding support for a new endpoint.

Operational and security guidance
=================================

* Prefer HTTPS for any request that crosses a network you do not fully trust.
* Use strong, unique account passwords and never embed them in published examples, source control, or logs.
* Do not expose access tokens or public stream/download links in logs or screenshots. A signed link can grant access to a file to whoever possesses
  it.
* Avoid logging the internals of ``User`` objects because the password is held in memory for token refresh.
* ``StoreLimitless.close()`` is safe for stopping a locally managed server process; it does not stop a separately operated remote service.
* VLC auto-installation can require download/network access and elevated privileges. Install VLC yourself first if automatic installation is not
  appropriate for your environment.
* File and directory objects are snapshots of remote metadata. Refresh the listing after rename, move, trash, or restore operations if code depends on
  updated parent paths or metadata.

Dependencies and compatibility
==============================

The SDK uses ``requests`` for HTTP communication and ``tqdm`` for terminal progress bars. The package uses modern Python type syntax such as ``X |
Y``; use a Python version compatible with the project metadata. VLC is only needed for the default media-streaming behavior, not for listing,
uploading, or ordinary downloading. The package's ``pyproject.toml`` is the authority for its exact Python version and dependency constraints.

Public exports
==============

The following names are re-exported from this package for convenience:

* ``StoreLimitless``
* ``User``
* ``Directory`` and ``DirectoryResult``
* ``File``
* ``StoreLimitlessError``
* ``StoreLimitlessConnectionError``
* ``StoreLimitlessHTTPError``
* ``StoreLimitlessResponseError``
* ``FileTree``
* ``UploadTask``, ``UploadJob`` and ``TorrentPreview``

Import public objects directly from ``storelimitless`` as shown throughout this guide. Internal implementation modules are not required for normal
use.
"""

from .auth import StoreLimitless
from .directory import Directory, DirectoryResult
from .exception import (
    StoreLimitlessConnectionError,
    StoreLimitlessError,
    StoreLimitlessHTTPError,
    StoreLimitlessResponseError,
)
from .file import File
from .user import User
from .tree import FileTree
from .tasks import TorrentPreview, UploadJob, UploadTask

__all__ = [
    "StoreLimitless",
    "Directory",
    "DirectoryResult",
    "StoreLimitlessError",
    "StoreLimitlessConnectionError",
    "StoreLimitlessHTTPError",
    "StoreLimitlessResponseError",
    "File",
    "User",
]
