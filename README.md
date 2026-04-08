# takeout_to_hatena

`takeout_to_hatena.py` scans Google Takeout ZIP files and imports Chrome Reading List URLs into Hatena Bookmark.

## What it does

- Uses Google Drive API directly to discover `takeout-*.zip` files in a target folder.
- Reads `takeout/Chrome/リーディング リスト.html` inside each ZIP.
- Extracts URLs from `<A HREF="...">...</A>`.
- Adds each URL to Hatena Bookmark with:
  - comment: `[Read later]`
  - visibility: private (`private=1`)
- Skips URLs that are already bookmarked.
- Persists state in `.takeout_to_hatena_state.sqlite3`, including:
  - processed ZIP signatures
  - URLs confirmed as bookmarked (already existing or newly added)

## Requirements

```bash
pip install httpx Authlib google-auth
```

## Google Drive API setup

1. Enable Google Drive API in your Google Cloud project.
2. Prepare credentials:
   - Recommended: service account JSON (`--google-credentials-file`).
   - Or use Application Default Credentials (omit `--google-credentials-file`).
3. Share the Drive folder that contains `takeout-*.zip` files with the service account (if using service account).
4. Find the target folder ID from Google Drive URL (`https://drive.google.com/drive/folders/<FOLDER_ID>`).

### Required API scopes

This tool needs read access to list and download ZIP files:

- `https://www.googleapis.com/auth/drive.readonly`

If you pass `--google-drive-scopes`, include at least the scope above.

## OAuth setup

1. Create a Hatena OAuth app and get consumer key/secret.
2. Obtain and save access tokens (`oauth_token`, `oauth_token_secret`) into `token.json`.

   You can use the included helper script:

   ```bash
   export HATENA_CONSUMER_KEY='your_key'
   export HATENA_CONSUMER_SECRET='your_secret'

   python get_hatena_token.py --token-file ./token.json --open-browser
   ```

   The script prints an authorization URL, asks for `oauth_verifier`, and writes `token.json`.

## Usage

```bash
export HATENA_CONSUMER_KEY='your_key'
export HATENA_CONSUMER_SECRET='your_secret'

python takeout_to_hatena.py \
  --google-drive-folder-id your_drive_folder_id \
  --google-credentials-file ./service-account.json \
  --token-file ./token.json
```

Custom state DB path:

```bash
python takeout_to_hatena.py \
  --google-drive-folder-id your_drive_folder_id \
  --google-credentials-file ./service-account.json \
  --state-db ./state.sqlite3
```

Dry run:

```bash
python takeout_to_hatena.py \
  --google-drive-folder-id your_drive_folder_id \
  --google-credentials-file ./service-account.json \
  --dry-run
```
