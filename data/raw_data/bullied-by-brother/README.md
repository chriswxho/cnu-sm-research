# Post-search dataset: bullied by brother

Canonical Reddit and PullPush post-search inputs and derived outputs.

## Query

- Exact phrase: `bullied by brother`
- Reddit: `title:"bullied by brother" OR selftext:"bullied by brother"`
- PullPush: independent title and selftext exact-phrase searches
- PullPush root windows: 5 years; 100-result roots are recursively bisected
- PullPush request timeout: 120 seconds
- Start: `2005-01-01T00:00:00-08:00`
- Requested cutoff (exclusive): `2026-07-26T22:46:24.793657-07:00`
- PullPush searches stop at the end of 2025-Q2; effective cutoff: `2025-07-01T00:00:00-07:00`

## Source inputs

- `reddit-api-posts-{relevance,hot,top,new,comments}.json`
- `pullpush-api-title-windows.json`
- `pullpush-api-selftext-windows.json`

The Reddit files contain complete post records. The PullPush files retain response envelopes and resumable time-window metadata.

## Derived outputs

- `reddit-union-candidates.json`
- `reddit-filtered-posts.json`
- `reddit-posts.csv`
- `pullpush-union-candidates.json`
- `pullpush-filtered-posts.json`
- `pullpush-posts.csv`
- `comparison.json`

Both CSVs use:

```text
type,subreddit,title,url,datetime_pst,score,body,media_url
```

## Offline rebuild

```bash
cnu-sm-research/bin/python fetch_posts.py 'bullied by brother' --offline
```

Offline mode makes no API requests and fails if a source file or PullPush interval is missing.
