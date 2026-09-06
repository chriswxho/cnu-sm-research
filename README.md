# cnu-sm-research

Use `fetch_posts.py` to create an exact-phrase post dataset under
`data/raw_data/<hyphenated-query>/`:

```bash
cnu-sm-research/bin/python fetch_posts.py "sibling abuse"
```

Each dataset keeps Reddit post records by sort, PullPush response checkpoints
by query field, matching source-specific CSVs, and the derived comparison
separately. PullPush requests default to one start every 60 seconds and up to
three attempts for a failed root window. Initial PullPush roots span five
years and are recursively bisected only when the API returns its 100-result
maximum or rejects a broad window as too complex. PullPush collection is
capped at the end of 2025-Q2 (`2025-07-01` exclusive), so no
Q3-2025-or-later windows are requested. PullPush requests use a fixed
120-second read timeout. The existing example is
[`data/raw_data/sibling-abuse/`](data/raw_data/sibling-abuse/README.md).

Raw retrieval artifacts and the 8,812-row deduplicated source stay under
`data/raw_data/`. Processed handoff datasets, annotations, filter metadata,
audits, and review shards are grouped into Pacific-time-stamped directories
such as `data/2026-09-05_21-06-01/`. Intentionally held-out query pulls are
kept under `data/raw_data/withheld_queries/`.

All things related to social media research performed by CNU School of Medicine.

## Setup

### Prerequisites
- Requires Python 3.7+ probably. Haven't verified personally but it probably works.
- Download `keys.json` from [Google Drive](https://drive.google.com/file/d/1iy0SgMLE9nUbWr27QuKhzAIPczcJkCRO/view?usp=drive_link); request access.

In the Mac Terminal, enter these commands:
```
// Copy this code into your computer
git clone git@github.com:chriswxho/cnu-sm-research.git
cd cnu-sm-research

// Move `keys.json` into the newly created `cnu-sm-research` directory.

// Create a virtual environemnt to manage package deps.
python3 -m venv cnu-sm-research
source cnu-sm-research/bin/activate
pip3 install -r requirements.txt

// Open JupyterLab. It will create a webpage with a notebook UI,
// go to `playground.ipynb` to experiment!
jupyter lab
```

## API clients

The existing Reddit client remains available from the package root:

```python
from api import RedditRequestManager
```

PullPush can search archived comments across a subreddit without first
selecting a post:

```python
from api.pullpush import PullPushRequestManager

pullpush = PullPushRequestManager()
comments = pullpush.search_comments(
    query_term='"sibling abuse"',
    subreddit_name="CPTSD",
    num_results=100,
)
```

Use `search_comments_raw(...)` to retain PullPush response envelopes and
metadata. The client enforces limits of 30 requests per minute and 1,000 per
hour through a shared `.pullpush-rate-limit.sqlite3` ledger. The SQLite ledger
is reused by restarted and concurrent Python processes in this repository, so
their requests count against the same sliding windows. It cannot observe
PullPush requests made from other checkouts, machines, or clients sharing the
same public IP.

PullPush submission searches support independent title, body, and general
query filters. Set `num_results=None` to exhaust timestamp pagination within
the requested date window:

```python
posts = pullpush.search_submissions(
    title='"sibling abuse"',
    after=1469502000,
    before=1785034800,
    num_results=None,
)
```

Use `search_submissions_raw(...)` when the original PullPush page envelopes
and metadata need to be serialized for reproducibility.

For audit-grade bounded searches, use adaptive window splitting. PullPush
returns at most 100 rows per request, so a window returning all 100 rows—or
one that PullPush rejects as too complex—is bisected recursively until every
retained leaf window returns fewer than 100:

```python
audit_windows, posts = pullpush.search_submissions_adaptive(
    title='"sibling abuse"',
    start_utc=1104566400,
    end_utc=1785034800,
)
```

Each audit-window record contains the exact non-overlapping `[start, end)`
interval, split depth, split reason or saturation decision, and untouched API
response. If a one-second window is still saturated or too complex, the
client raises an error instead of silently claiming the results are
exhaustive. Adaptive responses are checkpointed individually, so a later
transport or rate-limit failure resumes from the remaining uncovered
intervals instead of repeating already completed child windows.

PullPush's quoted `title` and `selftext` filters are candidate searches, not a
drop-in equivalent of Reddit's quoted phrase search. They can return inflected
or non-adjacent matches such as `siblings abused` or `sibling abuses`. Apply
the same local phrase predicate to the union from both APIs before comparing
coverage.
