from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

from . import config

MAX_RETRIES = 5 
_RETRY_DELAYS = [10, 30, 60, 120]


def _chunk_expected_size(idx: int, total: int, chunk_size: int) -> int:
    start = idx * chunk_size
    end = min((idx + 1) * chunk_size - 1, total - 1)
    return end - start + 1


def _load_state(state_file: Path, num_chunks: int, chunk_size: int, total: int) -> set[int]:
    """Return set of chunk indices recorded as complete, or empty set on any mismatch."""
    if not state_file.exists():
        return set()
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        if (
            data.get("num_chunks") != num_chunks
            or data.get("chunk_size") != chunk_size
            or data.get("total") != total
        ):
            return set()
        return set(data.get("completed_chunks", []))
    except Exception:
        return set()


def _save_state(
    state_file: Path,
    completed: set[int],
    num_chunks: int,
    chunk_size: int,
    total: int,
) -> None:
    content = json.dumps({
        "num_chunks": num_chunks,
        "chunk_size": chunk_size,
        "total": total,
        "completed_chunks": sorted(completed),
    })
    tmp = state_file.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    try:
        tmp.replace(state_file)
    except OSError:
        # Windows may deny the atomic rename (e.g. antivirus holds the .tmp file);
        # fall back to a direct write safe because callers hold state_file_lock.
        state_file.write_text(content, encoding="utf-8")
        tmp.unlink(missing_ok=True)


def _verify_chunk(chunk_file: Path, expected_size: int) -> bool:
    try:
        return chunk_file.exists() and chunk_file.stat().st_size == expected_size
    except OSError:
        return False


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, requests.HTTPError):
        return exc.response is not None and exc.response.status_code in (429, 500, 502, 503, 504)
    return isinstance(exc, (requests.ConnectionError, requests.Timeout))


class _SharedUrl:
    """Thread-safe URL holder that refreshes itself via the Graph API on demand."""

    def __init__(self, initial: str, refresh_fn=None) -> None:
        self._url = initial
        self._refresh_fn = refresh_fn
        self._lock = threading.Lock()

    def get(self) -> str:
        return self._url

    def refresh(self) -> str:
        if not self._refresh_fn:
            return self._url
        with self._lock:
            self._url = self._refresh_fn()
        return self._url


def _download_chunk(
    shared_url: _SharedUrl,
    start: int,
    end: int,
    dest: Path,
    advance_fn,
    retreat_fn,
    on_complete,
) -> None:
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        bytes_written = 0
        try:
            resp = requests.get(
                shared_url.get(),
                headers={"Range": f"bytes={start}-{end}"},
                stream=True,
                timeout=120,
            )
            resp.raise_for_status()
            with open(dest, "wb") as f:
                for block in resp.iter_content(chunk_size=256 * 1024):
                    if block:
                        f.write(block)
                        advance_fn(len(block))
                        bytes_written += len(block)
            on_complete()
            return
        except Exception as exc:
            retreat_fn(bytes_written)
            if _is_transient(exc) and attempt < MAX_RETRIES - 1:
                last_exc = exc
                time.sleep(_RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)])
                shared_url.refresh()
                continue
            raise
    raise last_exc  # type: ignore[misc]


def _merge(tmp_dir: Path, out_file: Path, n: int, progress=None, task_id=None, total: int = 0) -> None:
    out_file.parent.mkdir(parents=True, exist_ok=True)
    if progress is not None and task_id is not None:
        progress.update(
            task_id,
            description=f"[cyan]Merging {out_file.name}[/cyan]",
            completed=0,
            total=max(total, 1),
            visible=True,
        )
    with open(out_file, "wb") as out:
        for i in range(n):
            p = tmp_dir / f"{i:06d}"
            with open(p, "rb") as f:
                data = f.read()
                out.write(data)
                if progress is not None and task_id is not None:
                    progress.advance(task_id, len(data))
    # Delete chunks only after the full file is written if the process is
    # killed mid-merge, all chunk files remain intact for the next run.
    for i in range(n):
        (tmp_dir / f"{i:06d}").unlink(missing_ok=True)


def cleanup_tmp(out_file: Path) -> None:
    """Remove temp chunks directory and all its contents (including state file)."""
    tmp_dir = out_file.parent / f".{out_file.name}.chunks"
    if tmp_dir.exists():
        for p in tmp_dir.iterdir():
            p.unlink(missing_ok=True)
        try:
            tmp_dir.rmdir()
        except OSError:
            pass

def download_one(
    file_info: dict,
    out_dir: Path,
    progress,
    state: dict,
    lock: threading.Lock,
    token: str | None = None,
) -> None:
    name = file_info["name"]
    total = file_info["size"]
    rel_path = file_info.get("rel_path", name)
    item_path = file_info.get("item_path")

    out_file = out_dir / rel_path
    out_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = out_file.parent / f".{name}.chunks"
    tmp_dir.mkdir(exist_ok=True)

    refresh_fn = None
    if item_path and token:
        from .graph import get_fresh_download_url
        refresh_fn = lambda: get_fresh_download_url(item_path, token)

    shared_url = _SharedUrl(file_info["download_url"], refresh_fn)
    num_chunks = max(1, -(-total // config.CHUNK_SIZE))

    # --- Resume: load state and verify each recorded chunk on disk ---
    state_file = tmp_dir / "state.json"
    state_file_lock = threading.Lock()

    raw_completed = _load_state(state_file, num_chunks, config.CHUNK_SIZE, total)
    completed_set: set[int] = {
        idx for idx in raw_completed
        if _verify_chunk(tmp_dir / f"{idx:06d}", _chunk_expected_size(idx, total, config.CHUNK_SIZE))
    }

    resuming = bool(completed_set)
    label = (
        f"[green]{name}[/green] [yellow](resuming {len(completed_set)}/{num_chunks} chunks)[/yellow]"
        if resuming
        else f"[green]{name}[/green]"
    )
    task_id = progress.add_task(label, total=total)

    def advance(n: int) -> None:
        progress.advance(task_id, n)
        with lock:
            state["downloaded_bytes"] += n

    def retreat(n: int) -> None:
        if n > 0:
            progress.advance(task_id, -n)
            with lock:
                state["downloaded_bytes"] -= n

    # Pre-advance progress for chunks we're skipping
    already_bytes = sum(_chunk_expected_size(i, total, config.CHUNK_SIZE) for i in completed_set)
    if already_bytes:
        advance(already_bytes)

    def on_chunk_complete(idx: int) -> None:
        with state_file_lock:
            completed_set.add(idx)
            _save_state(state_file, completed_set, num_chunks, config.CHUNK_SIZE, total)

    pending = [i for i in range(num_chunks) if i not in completed_set]

    try:
        with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as pool:
            futures = [
                pool.submit(
                    _download_chunk,
                    shared_url,
                    i * config.CHUNK_SIZE,
                    min((i + 1) * config.CHUNK_SIZE - 1, total - 1),
                    tmp_dir / f"{i:06d}",
                    advance,
                    retreat,
                    lambda i=i: on_chunk_complete(i),
                )
                for i in pending
            ]
            for f in as_completed(futures):
                f.result()
    except Exception:
        # Preserve tmp_dir and state.json so the next run can resume
        progress.update(task_id, visible=False)
        raise

    with lock:
        state.setdefault("merging", set()).add(name)
    try:
        _merge(tmp_dir, out_file, num_chunks, progress=progress, task_id=task_id, total=total)
    finally:
        with lock:
            state.get("merging", set()).discard(name)
    state_file.unlink(missing_ok=True)

    try:
        tmp_dir.rmdir()
    except OSError:
        pass

    progress.update(task_id, visible=False)

    with lock:
        state["files_done"] += 1