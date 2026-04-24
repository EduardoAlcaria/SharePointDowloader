from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from rich.progress import Progress, TaskID

from .config import CHUNK_SIZE, MAX_WORKERS


def _download_chunk(
    url: str,
    start: int,
    end: int,
    dest: Path,
    advance_fn,
) -> None:
    resp = requests.get(
        url,
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


def _merge(tmp_dir: Path, out_file: Path, n: int) -> None:
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with open(out_file, "wb") as out:
        for i in range(n):
            p = tmp_dir / f"{i:06d}"
            with open(p, "rb") as f:
                out.write(f.read())
            p.unlink()


def cleanup_tmp(out_file: Path) -> None:
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
    progress: Progress,
    state: dict,
    lock: threading.Lock,
) -> None:
    name = file_info["name"]
    dl_url = file_info["download_url"]
    total = file_info["size"]
    rel_path = file_info.get("rel_path", name)

    out_file = out_dir / rel_path
    out_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = out_file.parent / f".{name}.chunks"
    tmp_dir.mkdir(exist_ok=True)

    num_chunks = max(1, -(-total // CHUNK_SIZE))
    task_id = progress.add_task(f"[green]{name}", total=total)

    def advance(n: int) -> None:
        progress.advance(task_id, n)
        with lock:
            state["downloaded_bytes"] += n

    try:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = [
                pool.submit(
                    _download_chunk,
                    dl_url,
                    i * CHUNK_SIZE,
                    min((i + 1) * CHUNK_SIZE - 1, total - 1),
                    tmp_dir / f"{i:06d}",
                    advance,
                )
                for i in range(num_chunks)
            ]
            for f in as_completed(futures):
                f.result()
    except Exception:
        cleanup_tmp(out_file)
        progress.update(task_id, visible=False)
        raise

    _merge(tmp_dir, out_file, num_chunks)

    try:
        tmp_dir.rmdir()
    except OSError:
        pass

    progress.update(task_id, visible=False)

    with lock:
        state["files_done"] += 1
