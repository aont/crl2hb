# Takeout Reading List -> Hatena Bookmark Sync

This tool checks Google Drive for newly added Google Takeout ZIP files and syncs URLs from Chrome Reading List to Hatena Bookmark.

## What it does

1. Looks for new ZIP files under a folder named `Takeout` with names matching `takeout-*.zip`.
2. Reads `/takeout/Chrome/リーディング リスト.html` from each new ZIP.
3. Extracts URLs from anchor tags such as `<A HREF="url">title</A>`.
4. Adds each URL to Hatena Bookmark as a private bookmark with comment `[あとで読む]`.
5. Skips URLs that are already bookmarked.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Set environment variables:

```bash
export GOOGLE_SERVICE_ACCOUNT_FILE=/path/to/service-account.json
export HATENA_ACCESS_TOKEN=<your_hatena_oauth_access_token>
```

## Run

```bash
python sync_takeout_to_hatena.py
```

Dry-run mode:

```bash
python sync_takeout_to_hatena.py --dry-run --verbose
```

## Notes

- Processed ZIP files are tracked in `.state/synced_takeout_files.json`.
- If a ZIP fails to parse, it will be retried next run.
- You can override the Hatena API base URL with `HATENA_ENDPOINT_BASE`.
