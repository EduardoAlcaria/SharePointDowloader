import os
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console

load_dotenv()

CLIENT_ID = os.environ.get("SP_CLIENT_ID")
AUTHORITY = "https://login.microsoftonline.com/common"
SCOPES = ["https://graph.microsoft.com/Files.Read.All"]
TOKEN_CACHE = Path.home() / ".sp_dl_token.json"

CHUNK_SIZE = 8 * 1024 * 1024
MAX_WORKERS = 8

console = Console()
