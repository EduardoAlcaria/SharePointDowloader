import base64
import re
from typing import List
from urllib.parse import urlparse, unquote, quote

import requests


def _graph_get(path: str, token: str, **kwargs) -> dict:
    resp = requests.get(
        f"https://graph.microsoft.com/v1.0{path}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
        **kwargs,
    )
    if resp.status_code == 401:
        raise RuntimeError("Access denied (401). Delete ~/.sp_dl_token.json and try again.")
    if resp.status_code == 403:
        raise RuntimeError("Permission denied (403). The link requires a higher access level.")
    if resp.status_code == 404:
        raise RuntimeError("Link not found (404). Check the URL.")
    resp.raise_for_status()
    return resp.json()


def get_fresh_download_url(item_path: str, token: str) -> str:
    data = _graph_get(item_path, token)
    dl_url = data.get("@microsoft.graph.downloadUrl")
    if not dl_url:
        raise RuntimeError("Could not refresh download URL from Graph API.")
    return dl_url


def _parse_sharepoint_r_url(url: str):
    """Parse a /:x:/r/ direct-path SharePoint URL into (hostname, site_path, item_path)."""
    parsed = urlparse(url)
    if not parsed.netloc.endswith(".sharepoint.com"):
        return None
    path = unquote(parsed.path)
    m = re.match(r"^/:[a-zA-Z]:/r(/sites/[^/]+)(/.+)$", path)
    if not m:
        return None
    site_path = m.group(1)
    parts = [p for p in m.group(2).split("/") if p]
    if len(parts) < 2:
        return None
    # parts[0] is the library name (e.g. "Shared Documents"); strip it
    return parsed.netloc, site_path, "/".join(parts[1:])


def _resolve_via_site_path(
    hostname: str, site_path: str, item_path: str, token: str, verbose: bool = False
) -> dict:
    from .config import console

    api_site = f"/sites/{hostname}:{site_path}"
    if verbose:
        console.print(f"[dim]  [graph] GET {api_site}[/dim]")
    site_data = _graph_get(api_site, token)
    site_id = site_data["id"]

    encoded_item = quote(item_path)
    api_item = f"/sites/{site_id}/drive/root:/{encoded_item}"
    if verbose:
        console.print(f"[dim]  [graph] GET {api_item}[/dim]")
    data = _graph_get(api_item, token)

    if "folder" in data:
        if verbose:
            console.print(f"[dim]  [resolve] ✓ resolved via site-path  name={data['name']}  type=folder[/dim]")
        return {
            "type": "folder",
            "name": data["name"],
            "drive_id": data["parentReference"]["driveId"],
            "item_id": data["id"],
        }

    dl_url = data.get("@microsoft.graph.downloadUrl")
    if not dl_url:
        raise RuntimeError(
            "Download URL not returned by Graph API.\n"
            "You may need to register an Azure AD app with Files.Read.All permission."
        )
    if verbose:
        console.print(
            f"[dim]  [resolve] ✓ resolved via site-path  name={data['name']}  size={data.get('size', 0)}[/dim]"
        )
    return {
        "type": "file",
        "name": data["name"],
        "size": int(data.get("size", 0)),
        "download_url": dl_url,
        "item_path": f"/sites/{site_id}/drive/items/{data['id']}",
        "rel_path": data["name"],
    }


def resolve_link(url: str, token: str, verbose: bool = False) -> dict:
    from .config import console

    # For /:x:/r/ direct-path links, resolve via site+drive API (more reliable)
    parsed = _parse_sharepoint_r_url(url)
    if parsed:
        hostname, site_path, item_path = parsed
        if verbose:
            console.print(f"[dim]  [resolve] Detected /:x:/r/ direct-path URL[/dim]")
            console.print(f"[dim]  [resolve]   hostname  = {hostname}[/dim]")
            console.print(f"[dim]  [resolve]   site_path = {site_path}[/dim]")
            console.print(f"[dim]  [resolve]   item_path = {item_path}[/dim]")
        try:
            return _resolve_via_site_path(hostname, site_path, item_path, token, verbose)
        except Exception as exc:
            if verbose:
                console.print(f"[dim]  [resolve] site-path failed ({exc}), falling back to /shares[/dim]")

    encoded = "u!" + base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    shares_path = f"/shares/{encoded}/driveItem"
    if verbose:
        console.print(f"[dim]  [graph] GET {shares_path}[/dim]")
    data = _graph_get(shares_path, token)

    if "folder" in data:
        return {
            "type": "folder",
            "name": data["name"],
            "drive_id": data["parentReference"]["driveId"],
            "item_id": data["id"],
        }

    dl_url = data.get("@microsoft.graph.downloadUrl")
    if not dl_url:
        raise RuntimeError(
            "Download URL not returned by Graph API.\n"
            "You may need to register an Azure AD app with Files.Read.All permission."
        )
    if verbose:
        console.print(
            f"[dim]  [resolve] ✓ resolved via /shares  name={data['name']}  size={data.get('size', 0)}[/dim]"
        )
    return {
        "type": "file",
        "name": data["name"],
        "size": int(data.get("size", 0)),
        "download_url": dl_url,
        "item_path": shares_path,
        "rel_path": data["name"],
    }


def list_folder_files(drive_id: str, item_id: str, token: str, rel_path: str = "") -> List[dict]:
    files = []
    url = f"/drives/{drive_id}/items/{item_id}/children"

    while url:
        data = _graph_get(url, token)
        for item in data.get("value", []):
            item_rel = f"{rel_path}/{item['name']}" if rel_path else item["name"]
            if "folder" in item:
                files.extend(list_folder_files(drive_id, item["id"], token, item_rel))
            elif "file" in item:
                dl_url = item.get("@microsoft.graph.downloadUrl")
                if dl_url:
                    files.append({
                        "type": "file",
                        "name": item["name"],
                        "rel_path": item_rel,
                        "size": int(item.get("size", 0)),
                        "download_url": dl_url,
                        "item_path": f"/drives/{drive_id}/items/{item['id']}",
                    })
        url = data.get("@odata.nextLink")
        if url:
            url = url.replace("https://graph.microsoft.com/v1.0", "")

    return files
