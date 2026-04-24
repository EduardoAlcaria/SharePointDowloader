# SharePoint Downloader

Two CLI tools for working with private SharePoint links: a fast parallel downloader and a recursive file lister.

## Tools

| Command | Script | Description |
|---|---|---|
| `sp-dl` | — | Download files/folders |
| `python sp_ls.py` | `sp_ls.py` | List and filter files without downloading |

## Features

- Works with both file and folder sharing links
- Recursive folder traversal with live progress output
- Parallel chunked downloads using HTTP Range requests
- Filter files by name or folder path (case-insensitive)
- Token caching: authenticates once via Microsoft device-code flow

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Downloader — `sp-dl`

```bash
sp-dl "<sharepoint-url>" [options]
```

### Options

| Flag | Default | Description |
|---|---|---|
| `-o, --output` | `.` | Output directory |
| `-w, --workers` | `8` | Parallel connections per file |
| `-c, --chunk-mb` | `8` | Chunk size in MB |
| `-x, --extract` | — | Extract ZIP files after download |
| `--client-id` | `$SP_CLIENT_ID` | Custom Azure AD app client ID |
| `--reset-auth` | — | Clear cached credentials and re-authenticate |

### Examples

```bash
# Download a file
sp-dl "https://company.sharepoint.com/:u:/g/..." -o ~/Downloads

# Download an entire folder with 16 parallel connections
sp-dl "https://company.sharepoint.com/:f:/g/..." -o ~/Downloads -w 16 -c 16

# Download and extract ZIP files automatically
sp-dl "https://company.sharepoint.com/..." -o ~/Downloads -x

# Re-authenticate (e.g. to switch accounts)
sp-dl "..." --reset-auth
```

## File Lister — `sp_ls.py`

Lists all files in a SharePoint folder recursively, printing each folder and file as it is scanned. No files are downloaded.

```bash
python sp_ls.py "<sharepoint-url>" [options]
```

### Options

| Flag | Default | Description |
|---|---|---|
| `-f, --filter TERM` | — | Filter results by term (case-insensitive, matches full path) |
| `--client-id` | `$SP_CLIENT_ID` | Custom Azure AD app client ID |
| `--reset-auth` | — | Clear cached credentials and re-authenticate |

### Examples

```bash
# List all files in a folder
python sp_ls.py "https://company.sharepoint.com/:f:/g/..."

# Filter by filename
python sp_ls.py "https://company.sharepoint.com/..." -f "report.pptx"

# Filter by folder name (shows all files inside that folder)
python sp_ls.py "https://company.sharepoint.com/..." -f "2024"

# Multiple URLs
python sp_ls.py "https://..." "https://..." -f "budget"
```

The lister prints progress in real time as it traverses folders, then displays a summary table with path and size for all matched files.

## Authentication

Uses Microsoft device-code flow on first run:

1. A URL and a short code are printed in the terminal
2. Open the URL in any browser and enter the code
3. The token is cached at `~/.sp_dl_token.json` for future runs

### Enterprise tenants with Conditional Access

The built-in client ID (`d3590ed6-...`) is a public Microsoft client and works for most tenants. If yours has Conditional Access policies that block it:

1. Register an app in [Azure Entra ID](https://entra.microsoft.com)
2. Add `Files.Read.All` as a delegated Microsoft Graph permission
3. Enable "Allow public client flows"
4. Pass your client ID via flag or environment variable:

```bash
export SP_CLIENT_ID="your-client-id"
# or
python sp_ls.py "..." --client-id "your-client-id"
```

## Dependencies

- `msal` - Microsoft authentication
- `requests` - HTTP
- `rich` - terminal UI
- `python-dotenv` - environment variable loading
