"""Object-oriented wrappers for StoreLimitless upload tasks, jobs, and torrent previews.

The application persists upload work in Redis so task state can be inspected and
managed across devices. These wrappers keep the SDK's class-based style while
preserving the server as the source of truth: each object is a snapshot, and
callers explicitly refresh it when they need current progress.
"""

from __future__ import annotations

from math import isfinite
from typing import TYPE_CHECKING, Any, Iterator

from .auth import StoreLimitless
from .exception import StoreLimitlessResponseError

if TYPE_CHECKING:
    from .user import User


class UploadTask:
    """Represent a durable upload-tray task, also known as an upload batch.

    A task groups the user-visible work shown in the StoreLimitless Task tray.
    Its items commonly describe local files or a single remote import such as
    an HTTP URL, YouTube link, Telegram message link, or magnet torrent. The
    actual transfer and Redis-backed state are managed by the server.

    Args:
        user: Authenticated account that owns this task.
        batch_id: Stable identifier returned by the upload-batch creation route.
        data: Optional server JSON used to initialize the local snapshot.

    The current server has no dedicated GET-by-ID route for upload batches.
    Consequently, refresh() searches the list returned by
    GET /auth/upload-batches. This object does not poll in the background; the
    caller controls when refreshes occur.
    """

    def __init__(self, user: User, batch_id: str, data: dict[str, Any] | None = None) -> None:
        """Create a wrapper for an existing server-side upload batch."""
        if not batch_id.strip():
            raise ValueError("batch_id cannot be empty")
        self.user: User = user
        self.id: str = batch_id
        self.batch_id: str = batch_id
        self.data: dict[str, Any] = {}
        self._apply(data or {})

    def _apply(self, data: dict[str, Any]) -> None:
        """Merge a response snapshot and derive commonly used task fields.

        Unknown fields are retained in data. This lets clients inspect extra
        progress metadata from newer server versions without pretending to
        validate fields that this SDK version does not yet understand.
        """
        self.data.update(data)
        self.name: str = str(self.data.get("name") or "Upload task")
        self.kind: str = str(self.data.get("kind") or "file")
        self.status: str = str(self.data.get("status") or "queued")
        self.message: str = str(self.data.get("message") or "")
        self.directory: str = str(self.data.get("directory") or self.user.home.path)
        self.data_center: str = str(self.data.get("data_center") or "Discord")
        raw_items: Any = self.data.get("items", [])
        self.items: list[dict[str, Any]] = [dict(item) for item in raw_items if isinstance(item, dict)] if isinstance(raw_items, list) else []
        raw_torrent_files: Any = self.data.get("torrent_files", [])
        self.torrent_files: list[dict[str, Any]] = [dict(item) for item in raw_torrent_files if isinstance(item, dict)] if isinstance(raw_torrent_files, list) else []
        self.total_items: int = self._integer("total_items", len(self.items))
        self.completed_items: int = self._integer("completed_items", sum(item.get("status") == "completed" for item in self.items))
        self.current_index: int = self._integer("current_index")
        self.current_name: str = str(self.data.get("current_name") or "")

    def _integer(self, key: str, default: int = 0) -> int:
        """Read a non-negative integer from the stored task response."""
        value: Any = self.data.get(key, default)
        if isinstance(value, bool):
            return default
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return default

    def __repr__(self) -> str:
        """Return a concise representation without account credentials."""
        return f"UploadTask(batch_id={self.batch_id!r}, name={self.name!r}, status={self.status!r})"

    def refresh(self) -> UploadTask:
        """Replace this snapshot with the latest server-side task state.

        Returns:
            This same object with its public attributes refreshed.

        Raises:
            StoreLimitlessResponseError: The response is malformed or the task
                was not found in the task list, for example after dismissal.
        """
        response = StoreLimitless.request(self.user, "GET", "/auth/upload-batches")
        try:
            payload: Any = response.json()
            batches: Any = payload.get("batches") if isinstance(payload, dict) else None
            if not isinstance(batches, list):
                raise TypeError("batches must be a list")
            for item in batches:
                if isinstance(item, dict) and str(item.get("batch_id") or item.get("id") or "") == self.batch_id:
                    self._apply(item)
                    return self
        except (ValueError, TypeError) as error:
            raise StoreLimitlessResponseError("StoreLimitless server returned an invalid upload-batch listing") from error
        raise StoreLimitlessResponseError(f"Upload task {self.batch_id} was not present in the server task list")

    def update(self, **changes: Any) -> UploadTask:
        """Publish task progress or item-state changes to the server.

        Wraps PATCH /auth/upload-batches/{batch_id}. Supported current fields
        include status (queued/uploading/completed/error/cancelled), message,
        items, torrent_files, current_name, current_index, completed_items,
        current_job_id and item-level values such as item_index, item_status,
        item_message, item_transfer, item_total and item_progress. Values are
        sent as JSON; the server validates recognized fields and transitions.

        Args:
            **changes: Fields accepted by the server update endpoint.

        Returns:
            This object updated with the acknowledgement. Call refresh() to
            load the complete per-item snapshot after a patch.

        Raises:
            ValueError: No update fields were supplied.
            StoreLimitlessResponseError: The acknowledgement was malformed.
        """
        if not changes:
            raise ValueError("At least one upload-task field must be supplied")
        response = StoreLimitless.request(self.user, "PATCH", f"/auth/upload-batches/{self.batch_id}", json=changes)
        try:
            payload: Any = response.json()
            if not isinstance(payload, dict):
                raise TypeError("expected a JSON object")
            self._apply(payload)
            return self
        except (ValueError, TypeError) as error:
            raise StoreLimitlessResponseError("StoreLimitless server returned an invalid upload-task update response") from error

    def cancel(self) -> UploadTask:
        """Cancel the active child job and stop remaining queued task items.

        Cancellation can race with an item that just finished uploading. The
        server may preserve that item as completed while cancelling the rest.
        The response is a small acknowledgement; call refresh() to obtain all
        updated per-item details.
        """
        response = StoreLimitless.request(self.user, "POST", f"/auth/upload-batches/{self.batch_id}/cancel")
        try:
            payload: Any = response.json()
            if not isinstance(payload, dict):
                raise TypeError("expected a JSON object")
            self._apply(payload)
            return self
        except (ValueError, TypeError) as error:
            raise StoreLimitlessResponseError("StoreLimitless server returned an invalid upload-task cancellation response") from error

    def dismiss(self) -> None:
        """Remove this task from the shared Task tray.

        Dismissing removes task metadata and associated child-job records; it
        does not delete files that were already uploaded. The server rejects
        dismissal of an active task with HTTP 409, so call cancel() first when
        you intend to stop unfinished work.
        """
        StoreLimitless.request(self.user, "DELETE", f"/auth/upload-batches/{self.batch_id}")


class UploadJob:
    """Represent one child upload or server-side link-ingestion job.

    A job performs one local-file chunk transfer or one remote import. It is
    distinct from UploadTask, which groups queued items and child jobs. This
    class exposes independent download and upload counters, speeds and ETA
    values, ingestion type, file counts, and torrent item metadata when given
    by the server.

    Instances are snapshots rather than background polling workers. Call
    refresh() at a sensible interval to observe current progress.
    """

    def __init__(self, user: User, job_id: str, data: dict[str, Any] | None = None) -> None:
        """Create a wrapper for a server-issued upload job ID."""
        if not job_id.strip():
            raise ValueError("job_id cannot be empty")
        self.user: User = user
        self.id: str = job_id
        self.job_id: str = job_id
        self.data: dict[str, Any] = {}
        self._apply(data or {})

    def _apply(self, data: dict[str, Any]) -> None:
        """Merge a status response and normalize common progress metrics."""
        self.data.update(data)
        self.name: str = str(self.data.get("name") or "Upload")
        self.status: str = str(self.data.get("status") or "uploading")
        self.message: str = str(self.data.get("message") or "")
        self.transfer: int = self._integer("transfer")
        self.total: int = self._integer("total")
        self.download_transfer: int = self._integer("download_transfer")
        self.download_total: int = self._integer("download_total")
        self.download_active: bool = bool(self.data.get("download_active", False))
        self.download_speed: float = self._number("download_speed")
        self.upload_speed: float = self._number("upload_speed")
        self.download_eta: float | None = self._optional_number("download_eta")
        self.upload_eta: float | None = self._optional_number("upload_eta")
        self.ingestion_type: str = str(self.data.get("ingestion_type") or "")
        raw_counts: Any = self.data.get("file_counts", {})
        self.file_counts: dict[str, Any] = raw_counts if isinstance(raw_counts, dict) else {}
        self.last_updated_at: float = self._number("last_updated_at")
        raw_torrent_items: Any = self.data.get("torrent_items", [])
        self.torrent_items: list[dict[str, Any]] = [dict(item) for item in raw_torrent_items if isinstance(item, dict)] if isinstance(raw_torrent_items, list) else []

    def _integer(self, key: str) -> int:
        """Read a non-negative byte counter, normalizing missing or invalid values."""
        value: Any = self.data.get(key, 0)
        if isinstance(value, bool):
            return 0
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    def _number(self, key: str) -> float:
        """Read a finite non-negative numeric metric from the snapshot."""
        value: Any = self.data.get(key, 0)
        if isinstance(value, bool):
            return 0.0
        try:
            parsed: float = float(value)
            return parsed if isfinite(parsed) and parsed >= 0 else 0.0
        except (TypeError, ValueError):
            return 0.0

    def _optional_number(self, key: str) -> float | None:
        """Return None when the server has no estimate for an ETA."""
        return None if self.data.get(key) is None else self._number(key)

    def __repr__(self) -> str:
        """Return a concise representation without tokens or passwords."""
        return f"UploadJob(job_id={self.job_id!r}, name={self.name!r}, status={self.status!r})"

    def refresh(self) -> UploadJob:
        """Fetch the latest server-side status and progress metrics for this job.

        Returns this same object with the public attributes updated. Terminal
        status may remain available briefly after dismissal; once server-side
        retention expires, a status lookup returns HTTP 404. Avoid needlessly
        frequent polling.
        """
        response = StoreLimitless.request(self.user, "GET", f"/auth/upload-jobs/{self.job_id}/status")
        try:
            payload: Any = response.json()
            if not isinstance(payload, dict):
                raise TypeError("expected a JSON object")
            self._apply(payload)
            return self
        except (ValueError, TypeError) as error:
            raise StoreLimitlessResponseError("StoreLimitless server returned an invalid upload-job status") from error

    def cancel(self) -> UploadJob:
        """Cancel this job and publish its terminal status to other devices."""
        response = StoreLimitless.request(self.user, "POST", f"/auth/upload-jobs/{self.job_id}/cancel")
        try:
            payload: Any = response.json()
            if not isinstance(payload, dict):
                raise TypeError("expected a JSON object")
            self._apply(payload)
            return self
        except (ValueError, TypeError) as error:
            raise StoreLimitlessResponseError("StoreLimitless server returned an invalid upload-job cancellation response") from error

    def dismiss(self) -> None:
        """Remove this terminal job from the shared tray without deleting its file.

        The server returns HTTP 409 if the job is still active. Cancel it first
        if you intend to stop transfer work rather than merely removing history.
        """
        StoreLimitless.request(self.user, "DELETE", f"/auth/upload-jobs/{self.job_id}")

    def report_failure(self, message: str) -> UploadJob:
        """Report a client-side chunk-transfer failure for a local-file job.

        This endpoint is for clients that transmit local-file chunks, such as
        browser clients. It is not a generic failure mechanism for remote-link
        imports. The server sanitizes and truncates the message, stops the
        upload worker, and publishes an error state across devices.

        Args:
            message: Brief explanation of why the client-side transfer failed.

        Raises:
            ValueError: The message is empty or whitespace-only.
            StoreLimitlessResponseError: The acknowledgement is malformed.
        """
        if not message.strip():
            raise ValueError("message cannot be empty")
        response = StoreLimitless.request(self.user, "POST", f"/auth/upload-jobs/{self.job_id}/fail", json={"message": message})
        try:
            payload: Any = response.json()
            if not isinstance(payload, dict):
                raise TypeError("expected a JSON object")
            self._apply(payload)
            return self
        except (ValueError, TypeError) as error:
            raise StoreLimitlessResponseError("StoreLimitless server returned an invalid upload-job failure response") from error


class TorrentPreview:
    """Represent file metadata retrieved from a magnet link without downloading it.

    Each files entry contains the original zero-based torrent index, a safe
    relative path and the file size in bytes. Preserve the original index when
    selecting files for import: the preview can omit libtorrent padding files,
    so a visible row position is not necessarily the original torrent index.
    """

    def __init__(self, name: str, files: list[dict[str, Any]]) -> None:
        """Build a read-oriented wrapper around the torrent-preview response."""
        self.name: str = name
        self.files: list[dict[str, Any]] = [dict(item) for item in files]

    @property
    def total_size(self) -> int:
        """Return the sum of visible file sizes in bytes."""
        total: int = 0
        for item in self.files:
            value: Any = item.get("size", 0)
            if isinstance(value, bool):
                continue
            try:
                total += max(0, int(value))
            except (TypeError, ValueError):
                continue
        return total

    @property
    def indices(self) -> list[int]:
        """Return original torrent file indexes in preview order."""
        return [int(item["index"]) for item in self.files if isinstance(item.get("index"), int) and not isinstance(item.get("index"), bool)]

    def __iter__(self) -> Iterator[dict[str, Any]]:
        """Iterate through visible file metadata records in preview order."""
        return iter(self.files)

    def __repr__(self) -> str:
        """Summarize the preview without printing every file record."""
        return f"TorrentPreview(name={self.name!r}, files={len(self.files)}, total_size={self.total_size})"
