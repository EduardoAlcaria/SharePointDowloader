# SharePoint Downloader

Fast parallel downloader for large files and folders from private SharePoint links, with a Rich terminal UI.

## Features

- Works with both file and folder sharing links
- Parallel chunked downloads using HTTP Range requests
- Live progress: speed, ETA per file, ETA total, chunk counter
- Token caching: authenticates once via Microsoft device-code flow

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Usage

```bash
sp-dl "<sharepoint-url>" [options]

# Or without installing:
python3 sp_download.py "<sharepoint-url>" [options]
```

### Options

| Flag | Default | Description |
|---|---|---|
| `-o, --output` | `.` | Output directory |
| `-w, --workers` | `8` | Parallel connections per file |
| `-c, --chunk-mb` | `8` | Chunk size in MB |
| `--client-id` | `$SP_CLIENT_ID` | Custom Azure AD app client ID |
| `--reset-auth` | — | Clear cached credentials and re-authenticate |

### Examples

```bash
# Download a file
sp-dl "https://company.sharepoint.com/:u:/g/..." -o ~/Downloads

# Download an entire folder with 16 parallel connections
sp-dl "https://company.sharepoint.com/:f:/g/..." -o ~/Downloads -w 16 -c 16

# Re-authenticate (e.g. to switch accounts)
sp-dl "..." --reset-auth
```

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
4. Set your client ID via the environment variable:

```bash
export SP_CLIENT_ID="your-client-id"
```

## Dependencies

- `msal` - Microsoft authentication
- `requests` - HTTP
- `rich` - terminal UI
