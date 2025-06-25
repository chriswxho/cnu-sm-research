from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json

import pandas as pd

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
        # if "subreddit" not in data:
        #     # TODO: take care of the weird case in API side
        #     # `{'count': 0, 'name': 't1__', 'id': '_', 'parent_id': 't1_mz9gdku', 'depth': 10, 'children': []}`
        #     continue
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
    with open(filename, "w") as f:
        json.dump(data, f)

