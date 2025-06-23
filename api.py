from enum import Enum
import json
import os
import secrets

import requests
import requests.auth as auth


USER_AGENT = "CNU Social Media Research Client"
STATE = secrets.token_urlsafe(30)
REDDIT_ACCESS_TOKEN_URL: str = "https://www.reddit.com/api/v1/access_token"
REDDIT_OAUTH_BASE_URL: str = "https://oauth.reddit.com"

reddit_api_keys = json.loads("keys.json")
CLIENT_ID = reddit_api_keys["CLIENT_ID"]
SECRET_ID = reddit_api_keys["SECRET_ID"]


class SortBy(Enum):
    RELEVANCE = "relevance"
    HOT = "hot"
    TOP = "top"
    NEW = "new"
    COMMENTS = "comments"


def _get_access_token() -> str:
    client_auth = auth.HTTPBasicAuth(CLIENT_ID, SECRET_ID)
    post_data = {
        "grant_type": "client_credentials",
        "device_id": STATE,
    }
    headers = {"User-Agent": USER_AGENT}
    auth_response = requests.post(
        REDDIT_ACCESS_TOKEN_URL, 
        auth=client_auth,
        data=post_data,
        headers=headers
    )

    if auth_response.status_code != 200:
        raise requests.RequestException("Didn't get a 200 when attempting OAuth authorization").add_note(
            "\n".join([
                "-- Debug info --",
                f"\tClient ID: {CLIENT_ID}",
                f"\tResponse: {auth_response.content}",
                "-- End debug info --",
            ])
        )
    access_token = auth_response.json()["access_token"]
    return access_token


HEADERS = {
    "User-Agent": USER_AGENT,
    "Authorization": f"bearer {_get_access_token()}",
}


def build_search_endpoint_query(
    subreddit_name: str, query_term: str, sort_by: SortBy = SortBy.NEW
) -> str:
    query_str = "&".join([
        f"q={query_term}",
        f"sort_by={sort_by.value}",
        "limit=100",
        "restrict_sr=true",
    ])
    return REDDIT_OAUTH_BASE_URL + f"/r/{subreddit_name}/search?{query_str}"


def build_parent_comment_endpoint_query(post_fullname: str) -> str:
    return REDDIT_OAUTH_BASE_URL + f"/comments/{post_fullname}?depth=10"


def search_posts(subreddit_name: str, query_term: str, sort_by: SortBy = SortBy.NEW) -> dict[str, object]:
    query = build_search_endpoint_query(
        subreddit_name=subreddit_name,
        query_term=query_term,
        sort_by=sort_by,
    )
    search_resp = requests.get(query, headers=HEADERS)
    if search_resp.status_code != 200:
        raise requests.RequestException("Didn't get a 200 when attempting OAuth authorization").add_note(
            "\n".join([
                "-- Debug info --",
                f"\tClient ID: {CLIENT_ID}",
                f"\tResponse: {search_resp.content}",
                "-- End debug info --",
            ])
        )

    return search_resp.json()


def get_comments(post_fullname: str) -> dict[str, object]:
    query = build_parent_comment_endpoint_query(post_fullname=post_fullname)
    comments_resp = requests.get(query, headers=HEADERS)
    if comments_resp.status_code != 200:
         raise requests.RequestException("Didn't get a 200 when attempting OAuth authorization").add_note(
            "\n".join([
                "-- Debug info --",
                f"\tClient ID: {CLIENT_ID}",
                f"\tResponse: {comments_resp.content}",
                "-- End debug info --",
            ])
        )
    
    comments_resp_loaded = comments_resp.json()
    assert len(comments_resp_loaded) == 2, (
        f"Expected comment response JSON to have 2 elements but got {len(comments_resp_loaded)} elements"
    )
    _post_json, comments_json = comments_resp_loaded
    return comments_json