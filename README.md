# StoreLimitless

**StoreLimitless** is a Python SDK for working with a StoreLimitless server and its remote file system. It provides an
object-oriented interface for account registration and login, directory navigation, file uploads and URL imports,
downloads, media streaming, search, moving, and trash management.

The SDK communicates with a StoreLimitless server. It is not a standalone cloud storage service: you need a compatible
server and a configured storage backend. The package includes server executables for supported Linux and Windows
environments, which the SDK can launch when the configured local server is not already responding.

## Features

- Register accounts and authenticate with a StoreLimitless server.
- Browse virtual directories using `Directory` objects and familiar navigation methods.
- Create folders, search for files and folders, and inspect storage usage.
- Upload local files in chunks with a terminal progress bar.
- Import a file from an HTTP (S) URL without first downloading it to the Python machine.
- Download files to a local path with progress tracking.
- Create public stream/download links and launch media in VLC or another player.
- Rename and move individual files or folders, or use a batch move for multiple items.
- Move files and directories to trash, restore them, and permanently delete trashed items.
- Use exported exceptions to handle connection errors, HTTP errors, and malformed server responses.

## Requirements

- Python **3.10 or newer**.
- A compatible, reachable StoreLimitless server or a supported bundled server executable.
- An internet or network connection to the configured server.
- VLC for the default `File.stream()` media-player workflow. Listing, uploading, and ordinary downloads do not require
  VLC.

The SDK uses `requests` for HTTP requests and `tqdm` for terminal progress bars. These are installed as package
dependencies.

## Installation

Install the published package from PyPI:

```bash
python -m pip install --upgrade storelimitless
```

Check that the package is importable:

```bash
python -c "import storelimitless; print('StoreLimitless SDK imported successfully')"
```

## Quick start

The following example uses the bundled local server if needed. Replace the example username and password with your own
credentials.

```python
from storelimitless import StoreLimitless

# Check the default local server and start the bundled server if needed.
StoreLimitless.initialize()

try:
    user = StoreLimitless.login("your_username", "your_password")

    # List the immediate contents of the remote home directory.
    listing = user.home.ls

    for directory in listing.directories:
        print("Folder:", directory.path.name)

    for file in listing.files:
        print("File:", file.name, file.size, "bytes")

    # Create a remote folder and upload a local file into it.
    documents = user.home.mkdir("documents")
    uploaded = documents.upload("./report.pdf", data_center="Discord")
    print("Uploaded:", uploaded.name, uploaded.size)

    # Search across the account for files and directories.
    results = user.search("report")

    for file in results.files:
        print("Match:", file.directory.path / file.name)

    # Download the file to a local directory.
    saved_path = uploaded.download("./downloads/report.pdf")
    print("Saved to:", saved_path.resolve())

finally:
    # Stops a server process only when this SDK launched it.
    StoreLimitless.close()
```

`StoreLimitless.close()` does **not** stop a server that was already running independently or a remote server.

## Connecting to a server

Call `StoreLimitless.initialize()` before registering or logging in. The `server_ip` argument is the **base URL** of the
API, not a URL to a specific endpoint.

### Use the default local server

```python
from storelimitless import StoreLimitless

StoreLimitless.initialize()
```

The default base URL is `http://127.0.0.1:8000`. The SDK checks `GET /public/ready`. If the server is healthy, it reuses
it. Otherwise, it attempts to launch the bundled executable for the current operating system and waits for readiness.

### Use an already-running local or remote server

```python
from storelimitless import StoreLimitless

# Existing local server on a non-default port.
StoreLimitless.initialize(server_ip="http://127.0.0.1:9000")

# Example remote server. Use HTTPS over untrusted networks.
# StoreLimitless.initialize(server_ip="https://storage.example.com")
```

The configured server must already respond to the readiness check if you intend to use a remote service without
launching a local bundled executable. If the server is unavailable, initialization may attempt the bundled-server
startup process.

### Specify a server executable

Pass `server_path` if you need a particular local executable or the default bundled binary is not available:

```python
from pathlib import Path

from storelimitless import StoreLimitless

StoreLimitless.initialize(server_path=Path("/absolute/path/to/StoreLimitlessServer"), server_ip="http://127.0.0.1:8000")
```

The file must exist and be executable. On non-Windows systems, the SDK adds executable permission bits before starting
it. The SDK writes `server` to the child process's standard input and waits for the readiness endpoint, so the supplied
executable must support that launch protocol. The bundled executable layout may vary by package release.

## Account registration and login

Registration creates an account; it does not return an authenticated `User` object. Log in after registering.

```python
from storelimitless import StoreLimitless

StoreLimitless.initialize()

StoreLimitless.register(first_name="Ada", last_name="Lovelace", username="ada", password="use-a-strong-unique-password")

user = StoreLimitless.login("ada", "use-a-strong-unique-password")
print(user.username)
print(user.first_name, user.last_name)
```

A `User` represents the authenticated account. The SDK automatically includes the access token on authenticated
requests. The current client also keeps the supplied password in memory so it can try logging in again once if the
server explicitly returns an `Invalid token` response. Do not print, log, serialize, or commit credentials to source
control.

## Public API overview

All of the following classes and exceptions are re-exported from `storelimitless`, so normal applications do not need to
import internal modules.

```python
from storelimitless import (
    Directory,
    DirectoryResult,
    File,
    StoreLimitless,
    StoreLimitlessConnectionError,
    StoreLimitlessError,
    StoreLimitlessHTTPError,
    StoreLimitlessResponseError,
    User,
)
```

### `StoreLimitless`

This is a class-level API namespace and is not instantiated.

| Member                                                               | Description                                                                                                |
|----------------------------------------------------------------------|------------------------------------------------------------------------------------------------------------|
| `StoreLimitless.initialize(...)`                                     | Configure the API URL and reuse or launch a server. See [Connecting to a server](#connecting-to-a-server). |
| `StoreLimitless.register(first_name, last_name, username, password)` | Register an account. Returns `None` on success.                                                            |
| `StoreLimitless.login(username, password)`                           | Authenticate and return a `User`.                                                                          |
| `StoreLimitless.close()`                                             | Stop only the local process managed by this SDK.                                                           |
| `StoreLimitless.request(user, method, path, **kwargs)`               | Send an advanced/raw HTTP request and return a `requests.Response`.                                        |
| `StoreLimitless.API_URL`                                             | The currently configured base URL.                                                                         |

### `User`

| Member                                               | Description                                                                                                          |
|------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------|
| `user.username`, `user.first_name`, `user.last_name` | Basic account information.                                                                                           |
| `user.home`                                          | The account's remote home `Directory`, normally `/home`.                                                             |
| `user.metadata`                                      | Fetch metadata as a dictionary; currently contains `storage_used_bytes`.                                             |
| `user.storage_used_bytes`                            | Convenience property for storage used, in bytes. The server's current calculation includes active and trashed files. |
| `user.trash`                                         | Return a `DirectoryResult` containing trashed directories and files.                                                 |
| `user.search(query, trash=False)`                    | Search the account for files and directories by name. Set `trash=True` to search trashed items.                      |
| `user.move(items, destination)`                      | Batch-move `File` and/or `Directory` objects. Returns the server's per-item result dictionary.                       |

### `Directory`

A `Directory` represents a **remote StoreLimitless directory**, not a directory on the local computer.

| Member                                                 | Description                                                                                   |
|--------------------------------------------------------|-----------------------------------------------------------------------------------------------|
| `directory.path`, `directory.id`                       | Remote path and directory ID.                                                                 |
| `directory.modified_at`, `directory.deleted_at`        | Timestamps supplied by the server; either may be unavailable for some lightweight references. |
| `directory.ls`                                         | List immediate child directories and files, returning `DirectoryResult`.                      |
| `directory.cd(name)`                                   | Return a child directory, parent (`".."`), or home (`"~"` or `"/"`).                          |
| `directory / name`                                     | Shorthand for `directory.cd(name)`.                                                           |
| `directory.mkdir(name)`                                | Create a child directory and return its refreshed object.                                     |
| `directory.search(query, trash=False)`                 | Account-wide server-side search. It is not limited to the current subtree.                    |
| `directory.find(query, trash=False)`                   | Compatibility alias for `search`.                                                             |
| `directory.upload(source, data_center="Discord", ...)` | Upload a local path or import an HTTP(S) URL.                                                 |
| `directory.rename(name)`                               | Rename the directory; the server updates descendant paths.                                    |
| `directory.move(destination)`                          | Move the directory into an existing destination directory.                                    |
| `directory.rm()`                                       | Move the directory to the trash.                                                              |
| `directory.restore()`                                  | Restore a trashed directory.                                                                  |
| `directory.delete()`                                   | Permanently delete a directory from the trash and its trashed contents.                       |

Directory navigation accepts one child directory name at a time. Use multiple `cd()` calls to traverse nested folders.
Avoid using local paths when you intend to navigate the remote filesystem.

### `DirectoryResult`

Directory listings and search results return a `DirectoryResult`, which separates folders from files:

- `result.directories`: a list of `Directory` objects.
- `result.files`: a list of `File` objects.
- `result.directory`: the first directory, or `None` if there are no directories.
- `result.file`: the first file, or `None` if there are no files.
- `directories, files = result`: unpacking is supported in that order.

Example:

```python
listing = user.home.ls
folders, files = listing

first_folder = listing.directory
first_file = listing.file
```

### `File`

A `File` object represents a stored remote file and exposes metadata including `id`, `name`, `type`, `size`,
`modified_at`, `deleted_at`, `data_center`, and its parent `directory`.

| Method                                  | Description                                                                                         |
|-----------------------------------------|-----------------------------------------------------------------------------------------------------|
| `file.download(path=None, quiet=False)` | Download to a local path, showing progress by default; returns the destination as a `pathlib.Path`. |
| `file.public_link(download=False)`      | Create a signed public stream URL. Use `download=True` for a download URL.                          |
| `file.stream(application="vlc")`        | Open the public stream URL in the requested player executable and wait for the player to exit.      |
| `file.rename(name)`                     | Rename the remote file and update this object's name.                                               |
| `file.move(destination)`                | Move the file into an existing directory.                                                           |
| `file.rm()`                             | Move the file to the trash.                                                                         |
| `file.restore()`                        | Restore a trashed file.                                                                             |
| `file.delete()`                         | Permanently delete a file already in the trash.                                                     |

## Directory navigation and folder creation

```python
# Start from the remote home directory.
home = user.home

# Create nested directories.
projects = home.mkdir("projects")
reports = projects.mkdir("reports")

# Navigate down one level at a time.
current = home.cd("projects")
current = current / "reports"

# Navigate to the parent or home directory.
parent = reports.cd("..")
home_again = reports.cd("~")

# List a directory.
listing = reports.ls

for folder in listing.directories:
    print("Folder:", folder.path)

for file in listing.files:
    print("File:", file.name, file.size, "bytes")
```

Remote paths such as `/home/projects/reports` exist on the StoreLimitless server. They are separate from local paths
such as `./projects/reports` on your computer.

## Uploading files and importing URLs

`Directory.upload()` accepts either a local file path or an HTTP (S) URL. The destination is the `Directory` object on
which you call the method.

### Upload a local file

```python
from pathlib import Path

uploads = user.home.mkdir("uploads")

uploaded = uploads.upload(Path("./archive.zip"))
print(uploaded.name)
print(uploaded.size, "bytes")
```

### Choose the storage data center

The current SDK accepts `"Discord"` and `"Telegram"` for `data_center`:

```python
uploaded = uploads.upload(
    "./video.mkv",
    data_center="Telegram",
    quiet=False,
)
```

### Import from a URL

Pass a URL as a string. The server handles the import, so the file does not need to be downloaded into the Python
process's local working directory first.

```python
imported = uploads.upload(
    "https://example.com/files/archive.zip",
    data_center="Discord",
)
print(imported.name, imported.size)
```

The URL must be reachable by the server and supported by the server's link-import implementation. A successful
job-creation request is not by itself proof that the file has finished importing: the SDK waits for the job status and
then looks up the completed file in the destination listing.

### Upload tuning options

```python
uploaded = uploads.upload(
    "./large-video.mkv",
    data_center="Telegram",
    chunk_size=10 * 1024 * 1024,  # 10 MiB per chunk
    chunk_retries=3,
    poll_interval=0.2,
    job_timeout=3600,
    quiet=False,
)
```

| Argument        | Default            | Description                                                                                     |
|-----------------|--------------------|-------------------------------------------------------------------------------------------------|
| `source`        | Required           | Local `str`/`Path`, or an HTTP(S) URL supplied as a string.                                     |
| `data_center`   | `"Discord"`        | Storage destination: `"Discord"` or `"Telegram"`.                                               |
| `quiet`         | `False`            | Set to `True` to disable the terminal progress bar.                                             |
| `chunk_size`    | `10 * 1024 * 1024` | Local upload chunk size, in bytes. Must be positive.                                            |
| `chunk_retries` | `3`                | Number of attempts for a chunk after retryable connection or server errors. Must be at least 1. |
| `poll_interval` | `0.2`              | Seconds to wait between server-side job status checks. Must be non-negative.                    |
| `job_timeout`   | `3600.0`           | Maximum seconds to wait for the job to finish. Use `None` to wait without a time limit.         |

The local-file flow creates an upload job, transfers chunks with offsets and the full file size, polls the job status,
and returns the completed `File` object. Link imports create a server-side job and are also polled. Upload methods are
synchronous and wait for the operation to finish or raise an exception.

## Downloading files

Download to a specified local path:

```python
from pathlib import Path

file = next(item for item in user.home.ls.files if item.name == "archive.zip")
saved_path = file.download(Path("downloads/archive.zip"))
print("Saved to:", saved_path.resolve())
```

If `path` is omitted, the SDK saves the file under its remote filename in the current working directory. Parent
directories for an explicit path are created as needed. Downloads show a progress bar by default; pass `quiet=True` to
suppress it:

```python
file.download("downloads/archive.zip", quiet=True)
```

The method returns a `pathlib.Path` to the output file.

## Streaming and public links

Create a signed link without launching a player:

```python
stream_url = file.public_link()
download_url = file.public_link(download=True)

print(stream_url)
print(download_url)
```

Launch the stream in VLC (the default player):

```python
file.stream()
```

Use another installed player by passing its executable name or path:

```python
file.stream(application="mpv")
```

When VLC is not found, the SDK may attempt to install it on supported Windows or Debian/Ubuntu Linux systems. This can
require network access, package-manager access, or elevated privileges. Install VLC separately if automatic installation
is not appropriate for your environment.

**Security note:** Public stream and download URLs contain signed access tokens. Treat them like passwords: anyone
holding a valid link may be able to access the file permitted by that link. Do not commit them to source control, expose
them in logs, or share them publicly unless that is intended.

## Complete file tree

The main server exposes `GET /auth/tree`, which returns active and trashed files and folders plus the server-computed storage total in a single request. Use `user.tree` when a recursive walk through every directory would create unnecessary requests.

```python
from storelimitless import FileTree, StoreLimitless, User

StoreLimitless.initialize(server_ip="http://127.0.0.1:8000")
user: User = StoreLimitless.login("your_username", "your_password")

tree: FileTree = user.tree
print("Bytes stored:", tree.storage_used_bytes)

for folder in tree.active_directories:
    print("Folder:", folder.path)

for file in tree.active_files:
    print("File:", file.directory.path / file.name, file.size)

for file in tree.trashed_files:
    print("In trash:", file.name, file.deleted_at)
```

`FileTree` is a point-in-time snapshot. Request `user.tree` again after file operations when you need fresh data. The tree endpoint deliberately omits remote storage-provider links from file records.

## Streamed ZIP downloads

Download several files or one or more folders as a streamed ZIP64 archive. Folder contents are included recursively and keep their relative paths. The backend streams archive data from remote storage instead of building the entire archive as a server-side temporary file.

```python
from pathlib import Path
from storelimitless import Directory, File, User

home: Directory = user.home
listing = home.ls

# The server requires either at least one folder or at least two files.
selected: list[File | Directory] = list(listing.files[:2])
if len(selected) >= 2:
    archive: Path = user.download_zip(
        selected,
        path="./downloads/selected-files.zip",
        filename="selected-files.zip",
        quiet=False,
    )
    print("Archive saved to:", archive)

# A single folder is also a valid archive selection.
# projects: Directory = home.cd("projects")
# archive = user.download_zip([projects], "./downloads/projects.zip")
```

`user.create_zip_link(items)` can be used when your application needs the URL instead of writing the archive locally. It returns a pair of `(url, expires_in_seconds)`; the current server gives ZIP links a 15-minute lifetime. Valid selections are limited to 500 objects. When no folder is selected, select at least two files. ZIP links are bound to the current login session and are revoked when that session logs out, so treat the URL as a secret and do not persist it as a permanent share.

## Session-bound file links and logout

The existing `file.public_link()` method creates a public bearer link. For workflows that should stop working when the user logs out, use the session-bound methods instead:

```python
file = user.home.ls.files[0]

stream_url, stream_expires_in = file.private_stream_link()
download_url, download_expires_in = file.private_download_link()

print("Session stream expires in:", stream_expires_in)
print("Session download expires in:", download_expires_in)

# Logout revokes links bound to this login. Discard the old User object.
user.logout()
```

The backend advertises a 24-hour lifetime for these private links, but logout revokes them earlier. Public links made by `file.public_link()` are not revoked by this logout operation. After `user.logout()`, the local access token is cleared; sign in again through `StoreLimitless.login()` to obtain a usable `User` object.

## File metadata

Fetch the latest server-side metadata for a file using `file.metadata()`. Unlike the attributes already on a `File` instance, the returned dictionary represents a fresh response:

```python
metadata = file.metadata()
print(metadata["name"], metadata["type"], metadata["size"])
```

## Torrent preview and upload-job progress

The main server can resolve torrent metadata before downloading its payload, and its Task tray tracks download and upload progress separately. The SDK now wraps these responses as objects:

```python
from storelimitless import TorrentPreview, UploadJob, User

preview: TorrentPreview = user.preview_torrent(
    "magnet:?xt=urn:btih:REPLACE_WITH_A_VALID_INFO_HASH"
)
print(preview.name, preview.total_size)

for torrent_file in preview:
    # Preserve the original index; it may not match the visible row number.
    print(torrent_file["index"], torrent_file["path"], torrent_file["size"])

# Fetch known child jobs from the shared Task tray.
for job in user.upload_jobs:
    print(job.name, job.status, job.message)
    print("Downloaded:", job.download_transfer, "/", job.download_total)
    print("Uploaded:", job.transfer, "/", job.total)
    print("Download/upload rates:", job.download_speed, job.upload_speed)
    # job.refresh() updates the object's snapshot.
```

Torrent preview returns file metadata only. It does not start a download. The original zero-based torrent indices must be preserved when selecting files because padding files may be omitted from the visible preview.

## Task-tray jobs and batches

The task tray is backed by server-side state, so it can include work started on another signed-in device. `user.upload_tasks` lists task batches; `user.upload_jobs` lists child upload/import jobs. Instances are snapshots, and do not poll in the background.

```python
from storelimitless import UploadTask, UploadJob, User

for task in user.upload_tasks:
    print(task.batch_id, task.name, task.status, task.completed_items, "/", task.total_items)
    task.refresh()  # fetch the latest stored task state

for job in user.upload_jobs:
    print(job.job_id, job.name, job.status)
    if job.status == "uploading":
        job.cancel()  # cancel an active server-side job

# This registers tray metadata; it does not start the actual file transfer.
task: UploadTask = user.create_upload_task(
    "Reports",
    [{"name": "report.pdf", "size": 12345}],
    directory=user.home,
    data_center="Discord",
    kind="file",
)
print("Created task:", task.batch_id)
```

`UploadTask.update(...)` publishes task or per-item status. For example, callers may set `status`, `message`, `current_index`, `completed_items`, or per-item values such as `item_index`, `item_status`, `item_transfer`, `item_total`, and `item_progress`. `task.cancel()` stops the active item and cancels queued items. `task.dismiss()` removes a terminal task from the tray; it does not delete successfully uploaded files. Similarly, `job.dismiss()` removes a terminal child job, while `job.report_failure(message)` is specifically intended to report client-side chunk-transfer failure for a local-file job. The server rejects dismissal of active jobs/tasks.

A task created through `create_upload_task()` is only a task record. It does not create child jobs or transfer bytes on its own. Use the existing `Directory.upload()` or the appropriate supported import workflow to actually run transfer work.

## Searching files and directories

Search is account-wide, rather than limited to a directory subtree:

```python
results = user.search("report")

for folder in results.directories:
    print("Folder:", folder.path)
for file in results.files:
    print("File:", file.directory.path / file.name)
```

You can also call `search()` or its compatibility alias `find()` on a directory object. Despite the object on which it
is called, the search is global within the account.

Search the trash by setting `trash=True`:

```python
trashed_matches = user.search("report", trash=True)
```

An empty or whitespace-only search query raises `ValueError`.

## Rename and move operations

### Rename

```python
file.rename("report-final.pdf")
folder.rename("reports-archive")
```

File and directory names cannot be empty, `.` or `..`, and cannot contain `/` or `\\`. The server may reject additional
names or operations based on its own validation rules.

### Move a file or directory

The destination must be an existing remote directory. Pass a `Directory` object or a remote path:

```python
archive = user.home.mkdir("archive")
file.move(archive)
folder.move(archive)
```

Passing a `Directory` object allows the SDK to verify that the destination belongs to the same username. The server
remains responsible for permission checks and other path validation.

### Move multiple objects in one request

`User.move()` accepts an iterable of `File` and `Directory` objects and returns a dictionary containing the server's
per-item outcome. An HTTP-level success can still include individual failed items, so inspect the result:

```python
listing = user.home.ls
items = []

if listing.files:
    items.append(listing.files[0])
if listing.directories:
    items.append(listing.directories[0])

if items:
    result = user.move(items, destination=user.home.mkdir("archive"))
    print("Moved:", result.get("moved", []))
    print("Unchanged:", result.get("unchanged", []))
    print("Failed:", result.get("failed", []))
```

All items supplied to `User.move()` must belong to the authenticated account. At least one item is required.

## Trash, restore, and permanent deletion

The SDK keeps soft deletion separate from permanent deletion:

- `item.rm()` moves a file or directory to the trash.
- `item.restore()` restores an item from the trash.
- `item.delete()` permanently deletes an item that is already in the trash.
- `user.trash` lists trashed files and directories as a `DirectoryResult`.

Example:

```python
# Soft-delete a file.
file.rm()

# Inspect the trash.
trash = user.trash
for trashed_file in trash.files:
    print(trashed_file.name, trashed_file.deleted_at)

# Restore a trashed file.
file.restore()

# Permanently delete only when you intend to remove it for good.
# file.delete()
```

Permanently deleting an item may not be reversible. Refresh directory and trash listings after changing an item's state
if your application relies on current server metadata.

## Storage metadata

```python
print("Storage used:", user.storage_used_bytes, "bytes")
print("Metadata:", user.metadata)
```

`user.metadata` currently returns a dictionary containing `storage_used_bytes`. The server's current usage calculation
includes active and trashed files, but does not count directory metadata.

## Error handling

The SDK-specific exceptions are available directly from `storelimitless`:

| Exception                       | Meaning                                                                                                   |
|---------------------------------|-----------------------------------------------------------------------------------------------------------|
| `StoreLimitlessError`           | Base class for errors raised by the SDK.                                                                  |
| `StoreLimitlessConnectionError` | Connection, request timeout, server startup, or upload-job timeout failure.                               |
| `StoreLimitlessHTTPError`       | The server returned an unsuccessful HTTP status. Inspect `error.status_code` and the message.             |
| `StoreLimitlessResponseError`   | The response was successful or received, but its JSON/schema/content did not match what the SDK expected. |

Example:

```python
from storelimitless import (
    StoreLimitless,
    StoreLimitlessConnectionError,
    StoreLimitlessError,
    StoreLimitlessHTTPError,
    StoreLimitlessResponseError,
)

try:
    StoreLimitless.initialize()
    user = StoreLimitless.login("your_username", "your_password")
    user.home.upload("./report.pdf")

except StoreLimitlessHTTPError as error:
    print("Server rejected the request:", error.status_code, str(error))

except StoreLimitlessConnectionError as error:
    print("Connection or timeout error:", error)

except StoreLimitlessResponseError as error:
    print("Unexpected server response:", error)

except StoreLimitlessError as error:
    print("StoreLimitless operation failed:", error)

finally:
    StoreLimitless.close()
```

HTTP status codes and exact messages depend on the server version. Handle exceptions rather than relying on an exact
error string.

## Advanced: raw HTTP requests

`StoreLimitless.request()` is available for advanced use cases or server endpoints that do not yet have a dedicated SDK
method. It returns a `requests.Response`, includes the authenticated user's bearer token when a `User` object is
supplied, and converts transport and non-success HTTP outcomes into SDK exceptions.

```python
from storelimitless import StoreLimitless

response = StoreLimitless.request(user, "GET", "/auth/metadata")
metadata = response.json()

# Standard requests keyword arguments can be forwarded.
response = StoreLimitless.request(
    user,
    "GET",
    "/auth/search",
    params={"query": "report", "trash": False},
    timeout=30,
)
```

Supported keyword arguments are those accepted by `requests.request()`, such as `params`, `json`, `headers`, `data`,
`stream`, and `timeout`. The helper expects a path relative to the configured base URL, such as `/auth/metadata`, not a
complete URL.

### Endpoint reference

These routes describe the server API used by the current SDK release. Most applications should use the higher-level
Python methods above; request and response schemas remain the responsibility of the server version you deploy.

| Method and route                           | Purpose                                                                        |
|--------------------------------------------|--------------------------------------------------------------------------------|
| `POST /auth/register`                      | Register an account.                                                           |
| `POST /auth/login`                         | Authenticate and return a token, account information, and home-directory data. |
| `GET /auth/metadata`                       | Fetch account storage metadata.                                                |
| `GET /auth/directory`                      | List a remote directory.                                                       |
| `POST /auth/create-folder`                 | Create a remote folder.                                                        |
| `GET /auth/search`                         | Search files and directories, including optionally trashed items.              |
| `GET /auth/trash`                          | List trashed objects.                                                          |
| `POST /auth/upload`                        | Create a local-file upload job.                                                |
| `POST /auth/upload-link`                   | Create a server-side URL-import job.                                           |
| `PUT /public/upload/{job_id}/chunk`        | Send an upload chunk.                                                          |
| `GET /public/upload/{job_id}/status`       | Poll upload-job progress and status.                                           |
| `POST /auth/file/{fid}/public-link`        | Create a signed link token for a file.                                         |
| `GET /public/stream/{token}`               | Stream a file through a signed link.                                           |
| `GET /public/download/{token}`             | Download a file through a signed link.                                         |
| `PATCH /auth/file/{fid}/rename`            | Rename a file.                                                                 |
| `PATCH /auth/directory/{did}/rename`       | Rename a directory.                                                            |
| `PATCH /auth/file/{fid}/move`              | Move one file.                                                                 |
| `PATCH /auth/directory/{did}/move`         | Move one directory.                                                            |
| `PATCH /auth/move`                         | Batch-move files and directories.                                              |
| `DELETE /auth/file/{fid}`                  | Move a file to the trash.                                                      |
| `DELETE /auth/directory/{did}`             | Move a directory to the trash.                                                 |
| `POST /auth/trash/file/{fid}/restore`      | Restore a trashed file.                                                        |
| `POST /auth/trash/directory/{did}/restore` | Restore a trashed directory.                                                   |
| `DELETE /auth/trash/file/{fid}`            | Permanently delete a trashed file.                                             |
| `DELETE /auth/trash/directory/{did}`       | Permanently delete a trashed directory.                                        |
| `GET /auth/tree` | Return the complete active and trashed file tree with the storage total. |
| `GET /auth/file/{fid}/metadata` | Fetch fresh metadata for a specific file. |
| `POST /auth/file/{fid}/private-stream-link` | Create a stream link bound to the current login session. |
| `POST /auth/file/{fid}/download-link` | Create a download link bound to the current login session. |
| `POST /auth/logout` | Revoke links bound to the current login session. |
| `POST /auth/files/zip-link` | Create a short-lived link for a streamed ZIP64 archive. |
| `GET /auth/torrent-preview` | Resolve magnet metadata without downloading torrent payload bytes. |
| `POST /auth/upload-batches` | Create a task-tray batch record. |
| `GET /auth/upload-batches` | List task-tray batch snapshots. |
| `PATCH /auth/upload-batches/{batch_id}` | Publish task and per-item progress. |
| `POST /auth/upload-batches/{batch_id}/cancel` | Cancel the current batch and queued items. |
| `DELETE /auth/upload-batches/{batch_id}` | Dismiss a terminal task from the tray. |
| `GET /auth/upload-jobs` | List upload/import jobs and current snapshots. |
| `GET /auth/upload-jobs/{job_id}/status` | Fetch download/upload progress, speeds, ETA and torrent status. |
| `POST /auth/upload-jobs/{job_id}/cancel` | Cancel a child upload/import job. |
| `DELETE /auth/upload-jobs/{job_id}` | Dismiss a terminal child job. |
| `POST /auth/upload-jobs/{job_id}/fail` | Report a client-side failure for a local-file upload. |

Authenticated routes require a valid bearer token, supplied by the SDK when you pass a `User` to
`StoreLimitless.request()`. The upload status and chunk routes are used by the SDK's upload workflow; use the matching
server's route definitions for the exact schemas and limits.

## Security and operational notes

- Use HTTPS whenever traffic crosses a network you do not fully trust.
- Use strong, unique passwords. Do not put credentials in source control, public examples, or logs.
- `User` holds the password in memory for a limited automatic re-authentication flow. Do not log or serialize its
  internal state.
- Treat public stream/download links as bearer secrets.
- `StoreLimitless.close()` only stops the local child process launched by this SDK. It does not shut down a remote or
  independently started server.
- The SDK's file and directory objects contain a snapshot of server metadata. Refresh a listing after a rename, move,
  trash, or restore operation when fresh parent paths or timestamps matter.
- Automatic VLC installation may download software or invoke a system package manager. Review that behavior for your
  environment and install VLC manually when needed.

## License

Use the license declared by the repository/package distribution. See the repository's `LICENSE` file for the applicable
terms.
