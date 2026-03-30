# takeout_to_hatena

`takeout_to_hatena.py` scans Google Takeout ZIP files and imports Chrome Reading List URLs into Hatena Bookmark.

## What it does

- Looks for new ZIP files matching `takeout-*.zip` under a mounted Google Drive Takeout directory.
- Reads `takeout/Chrome/リーディング リスト.html` inside each ZIP.
- Extracts URLs from `<A HREF="...">...</A>`.
- Adds each URL to Hatena Bookmark with:
  - comment: `[Read later]`
  - visibility: private (`private=1`)
- Skips URLs that are already bookmarked.
- Persists processed ZIP signatures in `.takeout_to_hatena_state.json`.

## Requirements

```bash
pip install requests requests-oauthlib
```

## OAuth setup

1. Create a Hatena OAuth app and get consumer key/secret.
2. Obtain and save access tokens (`oauth_token`, `oauth_token_secret`) into `token.json`.
   - The referenced gist can be used to bootstrap token acquisition.

## Usage

```bash
export HATENA_CONSUMER_KEY='your_key'
export HATENA_CONSUMER_SECRET='your_secret'

python takeout_to_hatena.py \
  --takeout-dir /path/to/gdrive/Takeout \
  --token-file ./token.json
```

Dry run:

```bash
python takeout_to_hatena.py --takeout-dir /path/to/gdrive/Takeout --dry-run
```
