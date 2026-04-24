import msal

from .config import AUTHORITY, CLIENT_ID, SCOPES, TOKEN_CACHE, console


def _load_cache() -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    if TOKEN_CACHE.exists():
        cache.deserialize(TOKEN_CACHE.read_text())
    return cache


def _save_cache(cache: msal.SerializableTokenCache) -> None:
    if cache.has_state_changed:
        TOKEN_CACHE.write_text(cache.serialize())


def get_token() -> str:
    from rich.panel import Panel

    cache = _load_cache()
    app = msal.PublicClientApplication(CLIENT_ID, authority=AUTHORITY, token_cache=cache)

    result = None
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])

    if not result:
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise RuntimeError(f"Device flow failed: {flow.get('error_description')}")

        console.print(Panel(
            f"[bold]1.[/bold] Open in your browser:\n"
            f"   [cyan underline]{flow['verification_uri']}[/cyan underline]\n\n"
            f"[bold]2.[/bold] Enter the code:\n"
            f"   [bold yellow]{flow['user_code']}[/bold yellow]",
            title="[yellow] Microsoft Login Required [/yellow]",
            border_style="yellow",
            padding=(1, 4),
        ))
        result = app.acquire_token_by_device_flow(flow)

    _save_cache(cache)

    if "access_token" not in result:
        raise RuntimeError(f"Authentication failed: {result.get('error_description')}")
    return result["access_token"]
