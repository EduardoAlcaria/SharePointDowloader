import base64
from typing import List

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


def resolve_link(url: str, token: str) -> dict:
    encoded = "u!" + base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    data = _graph_get(f"/shares/{encoded}/driveItem", token)

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
    return {
        "type": "file",
        "name": data["name"],
        "size": int(data.get("size", 0)),
        "download_url": dl_url,
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
                    })
        url = data.get("@odata.nextLink")
        if url:
            url = url.replace("https://graph.microsoft.com/v1.0", "")

    return files
