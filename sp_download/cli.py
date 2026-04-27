from __future__ import annotations

import argparse
import sys
import threading
import time
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.table import Table

from .auth import get_token
from .config import CHUNK_SIZE, CLIENT_ID, MAX_WORKERS, TOKEN_CACHE, console
from .downloader import download_one
from .graph import list_folder_files, resolve_link
from .ui import fmt_eta, fmt_size


class LiveDisplay:
    def __init__(self, state: dict, lock: threading.Lock, progress: Progress, t0: float) -> None:
        self._state = state
        self._lock = lock
        self._progress = progress
        self._t0 = t0
        self._prev_bytes = 0
        self._prev_t = t0
        self._speed = 0.0

    def __rich__(self) -> Group:
        now = time.time()
        with self._lock:
            downloaded = self._state["downloaded_bytes"]
            files_total = self._state["files_total"]
            files_done = self._state["files_done"]
            files_failed = self._state["files_failed"]
            total_bytes = self._state["total_bytes"]
            merging = set(self._state.get("merging", set()))

        dt = now - self._prev_t
        if dt >= 0.3:
            instant = (downloaded - self._prev_bytes) / dt
            self._speed = instant if self._speed == 0.0 else 0.7 * self._speed + 0.3 * instant
            self._prev_bytes = downloaded
            self._prev_t = now

        remaining = max(0, total_bytes - downloaded)
        eta_str = fmt_eta(remaining / self._speed) if self._speed > 100_000 else "[dim]–[/dim]"
        speed_str = f"[bold green]{fmt_size(int(self._speed))}/s[/bold green]" if self._speed > 0 else "[dim]–[/dim]"

        t = Table(box=box.SIMPLE, show_header=False, padding=(0, 2), expand=True)
        t.add_column(style="bold cyan", no_wrap=True)
        t.add_column(style="white", no_wrap=True, ratio=2)
        t.add_column(style="bold cyan", no_wrap=True)
        t.add_column(style="white", no_wrap=True, ratio=2)

        t.add_row(
            "Files",
            f"{files_done + files_failed}/{files_total}",
            "Speed",
            speed_str,
        )
        t.add_row(
            "Done / Failed",
            f"[green]{files_done}[/green] / [red]{files_failed}[/red]",
            "ETA",
            f"[bold yellow]{eta_str}[/bold yellow]",
        )
        t.add_row(
            "Downloaded",
            f"{fmt_size(downloaded)} / {fmt_size(total_bytes)}",
            "Elapsed",
            fmt_eta(now - self._t0),
        )
        if merging:
            t.add_row(
                "Merging",
                f"[bold yellow]{', '.join(sorted(merging))}[/bold yellow]",
                "",
                "",
            )

        panel = Panel(t, title="[bold green] SharePoint Downloader [/bold green]", border_style="green")
        return Group(panel, self._progress)


def _resolve_url(url: str, token: str, verbose: bool = False) -> list[dict]:
    item = resolve_link(url, token, verbose=verbose)
    if item["type"] == "folder":
        source_name = item["name"]
        files = list_folder_files(item["drive_id"], item["item_id"], token, source_name)
    else:
        source_name = item["name"]
        files = [item]
    for f in files:
        f["source_url"] = url
        f["source_name"] = source_name
    return files


def _validate_urls(urls: list[str]) -> list[str]:
    valid = []
    for url in urls:
        if not url.startswith("http"):
            console.print(f"[bold red]Invalid URL (skipping):[/bold red] {url!r}")
        else:
            valid.append(url)
    return valid


def _print_summary(
    succeeded: list,
    failed: list,
    downloaded_bytes: int,
    out_dir: Path,
    elapsed: float,
    avg_spd: float,
) -> None:
    tbl = Table(title="Download Summary", box=box.ROUNDED, expand=True)
    tbl.add_column("Status", width=8, justify="center")
    tbl.add_column("Source", style="magenta", no_wrap=True)
    tbl.add_column("File", style="white", no_wrap=False)
    tbl.add_column("Size", style="cyan", justify="right")
    tbl.add_column("Error", style="red", no_wrap=False)

    for file_info, _ in succeeded:
        tbl.add_row(
            "[bold green]OK[/bold green]",
            file_info.get("source_name", ""),
            file_info["rel_path"],
            fmt_size(file_info["size"]),
            "",
        )

    for file_info, error in failed:
        tbl.add_row(
            "[bold red]FAIL[/bold red]",
            file_info.get("source_name", ""),
            file_info["rel_path"],
            fmt_size(file_info["size"]),
            error or "",
        )

    console.print()
    console.print(tbl)

    border = "green" if not failed else ("red" if not succeeded else "yellow")
    status = (
        "[bold green]All downloads complete![/bold green]"
        if not failed
        else "[bold red]Some downloads failed.[/bold red]"
        if not succeeded
        else "[bold yellow]Completed with errors.[/bold yellow]"
    )

    console.print(Panel(
        f"{status}\n\n"
        f"  [cyan]Succeeded:[/cyan]  {len(succeeded)}\n"
        f"  [cyan]Failed:[/cyan]     {len(failed)}\n"
        f"  [cyan]Downloaded:[/cyan] {fmt_size(downloaded_bytes)}\n"
        f"  [cyan]Output:[/cyan]     {out_dir.resolve()}\n"
        f"  [cyan]Total time:[/cyan] {fmt_eta(elapsed)}\n"
        f"  [cyan]Avg speed:[/cyan]  {fmt_size(int(avg_spd))}/s",
        border_style=border,
        padding=(1, 2),
    ))


def extract_zip(zip_path: Path) -> None:
    extract_to = zip_path.parent / zip_path.stem
    extract_to.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as zf:
        members = zf.infolist()
        total_size = sum(m.file_size for m in members)

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=None),
        "[progress.percentage]{task.percentage:>5.1f}%",
        "•",
        DownloadColumn(),
        "•",
        TransferSpeedColumn(),
        expand=True,
    )
    with progress:
        task = progress.add_task(f"[yellow]Extracting {zip_path.name}", total=max(total_size, 1))
        with zipfile.ZipFile(zip_path, "r") as zf:
            for member in zf.infolist():
                zf.extract(member, extract_to)
                progress.advance(task, member.file_size)

    console.print(f"  [green]✓[/green] Extracted to [cyan]{extract_to}[/cyan]")


def run(urls: list[str], out_dir: Path, extract: bool = False, verbose: bool = False) -> None:
    urls = _validate_urls(urls)
    if not urls:
        console.print("[red]No valid URLs provided.[/red]")
        return

    token = get_token()

    files: list[dict] = []
    console.print(f"\n[bold blue]Resolving {len(urls)} link(s)...[/bold blue]")

    with ThreadPoolExecutor(max_workers=len(urls)) as pool:
        future_to_url = {pool.submit(_resolve_url, url, token, verbose): url for url in urls}
        for future in as_completed(future_to_url):
            url = future_to_url[future]
            try:
                resolved = future.result()
                files.extend(resolved)
                console.print(f"  [green]✓[/green] {url[:120]}  →  {len(resolved)} file(s)")
            except Exception as exc:
                console.print(f"  [red]✗[/red] {url[:120]}\n    [red]{exc}[/red]")

    if not files:
        console.print("\n[yellow]No files to download.[/yellow]")
        return

    total_bytes = sum(f["size"] for f in files)

    tbl = Table(title="Files to download", box=box.ROUNDED, expand=True)
    tbl.add_column("#", style="dim", width=4)
    tbl.add_column("Source", style="magenta", no_wrap=True)
    tbl.add_column("File", style="white", no_wrap=False)
    tbl.add_column("Size", style="cyan", justify="right")
    for i, f in enumerate(files, 1):
        tbl.add_row(str(i), f.get("source_name", ""), f["rel_path"], fmt_size(f["size"]))
    console.print()
    console.print(tbl)
    console.print(f"\n[bold cyan]Total:[/bold cyan] {len(files)} file(s) — {fmt_size(total_bytes)}\n")

    state = {
        "total_bytes": total_bytes,
        "downloaded_bytes": 0,
        "files_total": len(files),
        "files_done": 0,
        "files_failed": 0,
        "merging": set(),
    }
    lock = threading.Lock()
    t0 = time.time()

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=None),
        "[progress.percentage]{task.percentage:>5.1f}%",
        "•",
        DownloadColumn(),
        "•",
        TransferSpeedColumn(),
        "•",
        TimeRemainingColumn(),
        expand=True,
    )

    display = LiveDisplay(state, lock, progress, t0)

    files_by_link: dict[str, list[dict]] = defaultdict(list)
    for f in files:
        files_by_link[f["source_url"]].append(f)

    results: list[tuple[dict, str | None]] = []
    results_lock = threading.Lock()

    def process_link(link_files: list[dict]) -> None:
        for file_info in link_files:
            try:
                download_one(file_info, out_dir, progress, state, lock, token=token)
                with results_lock:
                    results.append((file_info, None))
            except Exception as exc:
                with lock:
                    state["files_failed"] += 1
                with results_lock:
                    results.append((file_info, str(exc)))

    with Live(display, console=console, refresh_per_second=8):
        with ThreadPoolExecutor(max_workers=len(files_by_link)) as pool:
            futures = [pool.submit(process_link, link_files) for link_files in files_by_link.values()]
            for f in as_completed(futures):
                f.result()

    elapsed = time.time() - t0
    succeeded = [r for r in results if r[1] is None]
    failed = [r for r in results if r[1] is not None]
    downloaded_bytes = sum(f["size"] for f, _ in succeeded)
    avg_spd = downloaded_bytes / elapsed if elapsed > 0 else 0

    _print_summary(succeeded, failed, downloaded_bytes, out_dir, elapsed, avg_spd)

    if extract:
        zips = [
            out_dir / f["rel_path"]
            for f, _ in succeeded
            if f["name"].lower().endswith(".zip")
        ]
        if zips:
            console.print(f"\n[bold blue]Extracting {len(zips)} ZIP file(s)...[/bold blue]")
            for zip_path in zips:
                console.rule(f"[bold]{zip_path.name}[/bold]")
                try:
                    extract_zip(zip_path)
                except Exception as exc:
                    console.print(f"  [red]✗[/red] Failed to extract {zip_path.name}: {exc}")


def list_main() -> None:
    import sp_download.config as cfg

    ap = argparse.ArgumentParser(
        description="List files in a SharePoint folder without downloading",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("url", nargs="+", help="SharePoint sharing URL(s)")
    ap.add_argument("-f", "--filter", dest="term", default="", help="Filter files by term (case-insensitive)")
    ap.add_argument("--client-id", default=CLIENT_ID, help="Override Azure AD client ID")
    ap.add_argument("--reset-auth", action="store_true", help="Clear cached credentials and re-authenticate")
    ap.add_argument("-v", "--verbose", action="store_true", help="Show resolution trace (Graph API calls)")

    args = ap.parse_args()
    cfg.CLIENT_ID = args.client_id

    if args.reset_auth and TOKEN_CACHE.exists():
        TOKEN_CACHE.unlink()
        console.print("[yellow]Auth cache cleared.[/yellow]\n")

    urls = _validate_urls(args.url)
    if not urls:
        console.print("[red]No valid URLs provided.[/red]")
        sys.exit(1)

    try:
        token = get_token()
    except Exception as exc:
        console.print(f"\n[bold red]Auth error:[/bold red] {exc}")
        sys.exit(1)

    all_files: list[dict] = []
    console.print(f"\n[bold blue]Resolving {len(urls)} link(s)...[/bold blue]")

    with ThreadPoolExecutor(max_workers=len(urls)) as pool:
        future_to_url = {pool.submit(_resolve_url, url, token, args.verbose): url for url in urls}
        for future in as_completed(future_to_url):
            url = future_to_url[future]
            try:
                resolved = future.result()
                all_files.extend(resolved)
                console.print(f"  [green]✓[/green] {url[:120]}  →  {len(resolved)} file(s)")
            except Exception as exc:
                console.print(f"  [red]✗[/red] {url[:120]}\n    [red]{exc}[/red]")

    if not all_files:
        console.print("\n[yellow]No files found.[/yellow]")
        return

    term = args.term.lower()
    if term:
        filtered = [f for f in all_files if term in f["rel_path"].lower()]
    else:
        filtered = all_files

    tbl = Table(
        title=f"Files{f' matching [bold]{args.term}[/bold]' if term else ''}",
        box=box.ROUNDED,
        expand=True,
    )
    tbl.add_column("#", style="dim", width=5, justify="right")
    tbl.add_column("Source", style="magenta", no_wrap=True)
    tbl.add_column("Path", style="white")
    tbl.add_column("Size", style="cyan", justify="right")

    for i, f in enumerate(filtered, 1):
        tbl.add_row(str(i), f.get("source_name", ""), f["rel_path"], fmt_size(f["size"]))

    console.print()
    console.print(tbl)

    total_bytes = sum(f["size"] for f in filtered)
    match_note = f" (filtered from {len(all_files)})" if term else ""
    console.print(
        f"\n[bold cyan]{len(filtered)} file(s){match_note} — {fmt_size(total_bytes)} total[/bold cyan]\n"
    )


def main() -> None:
    import sp_download.config as cfg

    ap = argparse.ArgumentParser(
        description="Fast parallel downloader for SharePoint files and folders",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("url", nargs="+", help="SharePoint sharing URL(s)")
    ap.add_argument("-o", "--output", default=".", help="Output directory")
    ap.add_argument("-w", "--workers", type=int, default=MAX_WORKERS, help="Parallel chunk connections per file")
    ap.add_argument("-c", "--chunk-mb", type=int, default=CHUNK_SIZE // (1024 * 1024), help="Chunk size in MB")
    ap.add_argument("--client-id", default=CLIENT_ID, help="Override Azure AD client ID")
    ap.add_argument("--reset-auth", action="store_true", help="Clear cached credentials and re-authenticate")
    ap.add_argument("-x", "--extract", action="store_true", help="Extract ZIP files after download")
    ap.add_argument("-v", "--verbose", action="store_true", help="Show resolution trace (Graph API calls)")

    args = ap.parse_args()

    cfg.CHUNK_SIZE = args.chunk_mb * 1024 * 1024
    cfg.MAX_WORKERS = args.workers
    cfg.CLIENT_ID = args.client_id

    if args.reset_auth and TOKEN_CACHE.exists():
        TOKEN_CACHE.unlink()
        console.print("[yellow]Auth cache cleared.[/yellow]\n")

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        run(args.url, out_dir, extract=args.extract, verbose=args.verbose)
    except KeyboardInterrupt:
        console.print("\n[red]Download cancelled.[/red]")
        sys.exit(1)
    except Exception as exc:
        console.print(f"\n[bold red]Error:[/bold red] {exc}")
        sys.exit(1)