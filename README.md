# takeout_to_hatena

`takeout_to_hatena.py` fetches Google Takeout ZIP files from Google Drive API and imports Chrome Reading List URLs into Hatena Bookmark.

## What it does

- Lists ZIP files matching `takeout-*.zip` from a specified Google Drive folder.
  - If folder ID is omitted, it automatically searches for a folder named `Takeout`
    directly under **My Drive root** and uses that folder.
- Downloads each new ZIP directly through Google Drive API.
- Reads `Takeout/Chrome/リーディング リスト.html` inside each ZIP.
- Extracts URLs from `<A HREF="...">...</A>`.
- Adds each URL to Hatena Bookmark with:
  - comment: `[あとで読む]`
  - visibility: private (`private=1`)
- Skips URLs that are already bookmarked.
- Persists state in `.takeout_to_hatena_state.sqlite3`, including:
  - processed ZIP signatures
  - URLs confirmed as bookmarked (already existing or newly added)

## Requirements

```bash
pip install httpx Authlib
```

## OAuth setup

### 1) Hatena OAuth token

1. Create a Hatena OAuth app and get consumer key/secret.
2. Obtain and save access tokens (`oauth_token`, `oauth_token_secret`) into `token.json`.

```bash
python get_hatena_token.py \
  --consumer-key 'your_hatena_key' \
  --consumer-secret 'your_hatena_secret' \
  --token-file ./token.json \
  --open-browser
```

### 2) Google OAuth token (Drive API)

1. In Google Cloud Console, create an OAuth client ID for Desktop app.
2. Use client ID/secret and run the helper script:

```bash
python get_google_token.py \
  --client-id 'your_google_client_id' \
  --client-secret 'your_google_client_secret' \
  --token-file ./google_token.json \
  --open-browser
```

Required API scope (minimum):

- `https://www.googleapis.com/auth/drive.readonly`

This scope is used to list and download Takeout ZIP files from Drive.

## Usage

`takeout_to_hatena.py` reads OAuth client credentials from a TOML config file (default: `config.toml`):

```toml
[hatena]
consumer_key = "your_hatena_key"
consumer_secret = "your_hatena_secret"

[google]
client_id = "your_google_client_id"
client_secret = "your_google_client_secret"
```

```bash
python takeout_to_hatena.py \
  --config-file ./config.toml \
  --hatena-token-file ./token.json \
  --google-token-file ./google_token.json
```

Explicit folder ID:

```bash
python takeout_to_hatena.py \
  --drive-folder-id 'google_drive_folder_id' \
  --hatena-token-file ./token.json \
  --google-token-file ./google_token.json
```

Custom state DB path:

```bash
python takeout_to_hatena.py \
  --state-db ./state.sqlite3
```

Dry run:

```bash
python takeout_to_hatena.py \
  --dry-run
```


## List files/folders in a Drive folder

You can also list the immediate children (files and folders) of any Google Drive folder ID:

```bash
python list_drive_folder.py \
  --google-client-id 'your_google_client_id' \
  --google-client-secret 'your_google_client_secret' \
  --folder-id 'google_drive_folder_id' \
  --google-token-file ./google_token.json
```

JSON output:

```bash
python list_drive_folder.py --folder-id 'google_drive_folder_id' --json
```
