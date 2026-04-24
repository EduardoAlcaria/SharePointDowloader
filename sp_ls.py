import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from rich import box
from rich.table import Table

from sp_download.auth import get_token
from sp_download.config import CLIENT_ID, TOKEN_CACHE, console
from sp_download.graph import _graph_get, resolve_link
from sp_download.ui import fmt_size


def _list_folder_verbose(
    drive_id: str,
    item_id: str,
    token: str,
    rel_path: str,
    source_name: str,
    depth: int = 0,
) -> list[dict]:
    indent = "  " * (depth + 1)
    console.print(f"{indent}[dim]Scanning[/dim] [blue]{rel_path or '/'}[/blue] ...")

    files: list[dict] = []
    url = f"/drives/{drive_id}/items/{item_id}/children"

    while url:
        data = _graph_get(url, token)
        for item in data.get("value", []):
            item_rel = f"{rel_path}/{item['name']}" if rel_path else item["name"]
            if "folder" in item:
                files.extend(
                    _list_folder_verbose(drive_id, item["id"], token, item_rel, source_name, depth + 1)
                )
            elif "file" in item:
                dl_url = item.get("@microsoft.graph.downloadUrl")
                if dl_url:
                    size = int(item.get("size", 0))
                    console.print(f"{indent}  [dim]file[/dim] [white]{item['name']}[/white] [cyan]{fmt_size(size)}[/cyan]")
                    files.append({
                        "type": "file",
                        "name": item["name"],
                        "rel_path": item_rel,
                        "size": size,
                        "download_url": dl_url,
                        "source_name": source_name,
                    })
        url = data.get("@odata.nextLink")
        if url:
            url = url.replace("https://graph.microsoft.com/v1.0", "")

    return files


def _resolve_url(url: str, token: str) -> tuple[str, list[dict]]:
    item = resolve_link(url, token)
    if item["type"] == "folder":
        console.print(f"\n  [bold green]→[/bold green] Folder [magenta]{item['name']}[/magenta]")
        files = _list_folder_verbose(item["drive_id"], item["item_id"], token, item["name"], item["name"])
        console.print(f"  [green]✓[/green] {len(files)} file(s) found in [magenta]{item['name']}[/magenta]")
    else:
        console.print(f"\n  [bold green]→[/bold green] File [magenta]{item['name']}[/magenta]")
        item["source_name"] = item["name"]
        files = [item]
    return url, files


def main() -> None:
    import sp_download.config as cfg

    ap = argparse.ArgumentParser(
        description="List all files in a SharePoint folder without downloading",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("url", nargs="+", help="SharePoint sharing URL(s)")
    ap.add_argument(
        "-f", "--filter",
        dest="term",
        default="",
        help="Filter results by term (case-insensitive, matches path/filename)",
    )
    ap.add_argument("--client-id", default=CLIENT_ID, help="Override Azure AD client ID")
    ap.add_argument("--reset-auth", action="store_true", help="Clear cached token and re-authenticate")

    args = ap.parse_args()
    cfg.CLIENT_ID = args.client_id

    if args.reset_auth and TOKEN_CACHE.exists():
        TOKEN_CACHE.unlink()
        console.print("[yellow]Auth cache cleared.[/yellow]\n")

    urls = [u for u in args.url if u.startswith("http")]
    invalid = [u for u in args.url if not u.startswith("http")]
    for u in invalid:
        console.print(f"[bold red]Invalid URL (skipping):[/bold red] {u!r}")

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

    for url in urls:
        try:
            _, files = _resolve_url(url, token)
            all_files.extend(files)
        except Exception as exc:
            console.print(f"\n  [red]✗[/red] {url[:80]}\n    [red]{exc}[/red]")

    if not all_files:
        console.print("\n[yellow]No files found.[/yellow]")
        return

    term = args.term.lower()
    filtered = (
        [f for f in all_files if term in f["rel_path"].lower()]
        if term
        else all_files
    )

    if not filtered:
        console.print(f"\n[yellow]No files match [bold]{args.term!r}[/bold].[/yellow]")
        return

    title = f"Files matching [bold]{args.term}[/bold]" if term else "Files"
    tbl = Table(title=title, box=box.ROUNDED, expand=True)
    tbl.add_column("#",      style="dim",     width=5, justify="right")
    tbl.add_column("Source", style="magenta", no_wrap=True)
    tbl.add_column("Path",   style="white")
    tbl.add_column("Size",   style="cyan",    justify="right")

    for i, f in enumerate(filtered, 1):
        tbl.add_row(str(i), f.get("source_name", ""), f["rel_path"], fmt_size(f["size"]))

    console.print()
    console.print(tbl)

    total_bytes = sum(f["size"] for f in filtered)
    match_note = f" (filtered from {len(all_files)})" if term else ""
    console.print(
        f"\n[bold cyan]{len(filtered)} file(s){match_note} — {fmt_size(total_bytes)} total[/bold cyan]\n"
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[red]Cancelled.[/red]")
        sys.exit(1)
