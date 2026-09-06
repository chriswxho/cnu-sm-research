from collections import defaultdict
from datetime import datetime, timedelta, timezone
from enum import Enum
import html
import json
import re
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

REDDIT_BASE_URL = "https://www.reddit.com"
PACIFIC_TIMEZONE = ZoneInfo("America/Los_Angeles")
QUOTE_CHARACTERS = "\"'“”‘’＂＇"

POST_DATA_FIELDS: list[str] = [
    "subreddit",
    "query",
    "id",
    "title",
    "selftext",
    "created",
    "author",
    "link_flair_text",
    "permalink",
    "score",
    "num_comments",
]

DATA_FIELDS_TO_COLNAMES: dict[str, str] = {
    "selftext": "body",
    "created": "date",
    "link_flair_text": "flair",
}

COMMENT_DATA_FIELDS: list[str] = [
    "subreddit",
    "id",
    "parent_id",
    "body",
    "created",
    "author",
    "permalink",
    "score",
]


class RedditResultType(str, Enum):
    POST = "post"
    COMMENT = "comment"


def contains_exact_phrase_ignoring_quotes(
    text: object,
    phrase: str,
) -> bool:
    """Match adjacent words after removing quote characters only.

    Other punctuation and whitespace are preserved, so ``sibling 'abuse'``
    matches ``sibling abuse`` while ``sibling, abuse`` and
    ``sibling  abuse`` do not.
    """
    normalized_text = html.unescape(str(text)).translate(
        str.maketrans("", "", QUOTE_CHARACTERS)
    )
    pattern = re.compile(
        rf"(?<!\w){re.escape(phrase)}(?!\w)",
        flags=re.IGNORECASE,
    )
    return pattern.search(normalized_text) is not None


# Requested CSV column -> field in each Reddit API `data` object.
# `type`, `title`, `body`, and `media_url` are endpoint-aware. Posts use their
# title, `selftext`, and destination/media URLs; comments leave `title` and
# `media_url` blank and use `body`.
RESEARCH_COLUMN_API_FIELD_MAP: dict[str, Optional[str]] = {
    "type": None,
    "subreddit": "subreddit",
    "title": "title",
    "url": "permalink",
    "datetime_pst": "created_utc",
    "score": "score",
    "body": None,
    "media_url": "url_overridden_by_dest",
}


def map_research_record(
    data: dict[str, object],
    result_type: RedditResultType,
) -> dict[str, object]:
    """Map one Reddit post/comment API `data` object to the research CSV schema."""
    created_timestamp = data.get("created_utc", data.get("created"))
    if created_timestamp is None:
        raise KeyError("Reddit API object is missing both `created_utc` and `created`")

    permalink = str(data["permalink"])
    if permalink.startswith("/"):
        permalink = f"{REDDIT_BASE_URL}{permalink}"

    created_pacific = datetime.fromtimestamp(
        float(created_timestamp),
        tz=timezone.utc,
    ).astimezone(PACIFIC_TIMEZONE)
    body_field = "selftext" if result_type == RedditResultType.POST else "body"
    media_url = ""
    if result_type == RedditResultType.POST:
        media_url = str(data.get("url_overridden_by_dest") or "")
        if not media_url and not data.get("is_self", False):
            media_url = str(data.get("url") or "")
        if media_url.startswith("/"):
            media_url = f"{REDDIT_BASE_URL}{media_url}"

    return {
        "type": result_type.value,
        "subreddit": data["subreddit"],
        "title": (
            data.get("title", "")
            if result_type == RedditResultType.POST
            else ""
        ),
        "url": permalink,
        "datetime_pst": created_pacific.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "score": data["score"],
        "body": data.get(body_field, ""),
        "media_url": media_url,
    }


def extract_research_data(
    data: list[dict[str, object]],
    result_type: RedditResultType,
    filename: str,
) -> None:
    """Write the research CSV schema from serialized Reddit API objects."""
    rows = [map_research_record(record, result_type) for record in data]
    df = pd.DataFrame(rows, columns=RESEARCH_COLUMN_API_FIELD_MAP)
    with open(filename, "w", encoding="utf-8", newline="") as csv_file:
        df.to_csv(csv_file, index=False)


def extract_research_data_from_raw(
    raw_filename: str,
    result_type: RedditResultType,
    csv_filename: str,
) -> None:
    """Regenerate the research CSV from a prior `extract_raw_data` JSON file."""
    with open(raw_filename, encoding="utf-8") as raw_file:
        data = json.load(raw_file)
    if not isinstance(data, list):
        raise TypeError("Expected the serialized Reddit API data to be a JSON list")
    extract_research_data(data, result_type, csv_filename)


def extract_post_data(posts_data: list[dict[str, object]], filename: str) -> None:
    posts_data_dict = defaultdict(list)
    for data in posts_data:
        for field in POST_DATA_FIELDS:
            value = data[field]
            if field == "created":
                value = str(datetime.fromtimestamp(int(value), timezone(offset=timedelta(hours=-8))))
            if field == "permalink":
                value = f"https://www.reddit.com{value}"
            fieldname = DATA_FIELDS_TO_COLNAMES.get(field, field)
            posts_data_dict[fieldname].append(value)
    
    df = pd.DataFrame(posts_data_dict)
    with open(filename, "w") as f:
        df.to_csv(f)
        

def extract_comment_data(comments_data: list[dict[str, object]], filename: str) -> None:
    comments_data_dict = defaultdict(list)
    for data in comments_data:
        for field in COMMENT_DATA_FIELDS:
            value = data[field]
            if field == "created":
                value = str(datetime.fromtimestamp(int(value), timezone(offset=timedelta(hours=-8))))
            if field == "permalink":
                value = f"https://www.reddit.com{value}"
            fieldname = DATA_FIELDS_TO_COLNAMES.get(field, field)
            comments_data_dict[fieldname].append(value)
    
    df = pd.DataFrame(comments_data_dict)
    with open(filename, "w") as f:
        df.to_csv(f)

def extract_raw_data(data: list[dict[str, object]], filename: str) -> None:
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
