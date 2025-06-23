from enum import Enum
import itertools
import json
import os
import secrets
from typing import Optional

import requests
import requests.auth as auth


USER_AGENT = "CNU Social Media Research Client"
STATE = secrets.token_urlsafe(30)
REDDIT_ACCESS_TOKEN_URL: str = "https://www.reddit.com/api/v1/access_token"
REDDIT_OAUTH_BASE_URL: str = "https://oauth.reddit.com"

# only for /api/morechildren
BATCH_SIZE: int = 100

if not os.path.exists("keys.json"):
    raise FileNotFoundError(
        f"File `keys.json` was not found in the current working directory {os.getcwd()}. "
        "Please ensure the `keys.json` file is placed in the same parent folder (should be `cnu-sm-research/`)."
    )
with open("keys.json", "rb") as keys_file:
    reddit_api_keys = json.load(keys_file)
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
        headers=headers,
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


# TODO: wrap this in a "session manager"
ACCESS_TOKEN = _get_access_token()
HEADERS = {
    "User-Agent": USER_AGENT,
    "Authorization": f"bearer {ACCESS_TOKEN}",
}


def build_search_endpoint_query(
    subreddit_name: str,
    query_term: str,
    sort_by: SortBy = SortBy.NEW,
    after_fullname: Optional[str] = None,
) -> str:
    query_params = [
        f"q={query_term}",
        f"sort_by={sort_by.value}",
        "limit=100",
        "restrict_sr=true",
    ]
    if after_fullname is not None:
        query_params.append(f"after={after_fullname}")
    query_str = "&".join(query_params)
    return REDDIT_OAUTH_BASE_URL + f"/r/{subreddit_name}/search?{query_str}"


def build_toplevel_comment_endpoint_query(post_id: str, comment_id: Optional[str] = None) -> str:
    if comment_id is not None:
        return REDDIT_OAUTH_BASE_URL + f"/comments/{post_id}/comment/{comment_id}?limit=500&sort=old"
    return REDDIT_OAUTH_BASE_URL + f"/comments/{post_id}?limit=500&sort=old&depth=10"

def build_more_children_comment_endpoint_query(post_id: str, comment_ids: list[str]) -> str:
    comment_ids_str = ",".join(comment_ids)
    query_params = [
        f"link_id=t3_{post_id}",
        "limit_children=false",
        "sort=old",
        "api_type=json",
        f"children={comment_ids_str}",
    ]
    query_str = "&".join(query_params)
    return REDDIT_OAUTH_BASE_URL + f"/api/morechildren?{query_str}"


def search_posts(
    subreddit_name: str,
    query_term: str,
    num_results: int = 1000,
    sort_by: SortBy = SortBy.NEW
) -> list[dict[str, object]]:
    query = build_search_endpoint_query( 
        subreddit_name=subreddit_name,
        query_term=query_term,
        sort_by=sort_by,
        after_fullname=None,
    )

    def _query_posts(query: str) -> dict[str, object]:
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
        search_json = search_resp.json()
        return search_json
    
    results = []
    seen_ids = set()
    while len(seen_ids) < num_results:
        print(f"- Query: {query}")
        search_json = _query_posts(query)
        results.append(search_json)
        seen_ids.update(
            result_post["data"]["id"] for result_post in search_json["data"]["children"]
        )
        after = search_json["data"]["after"]

        # we might have hit the max number of posts for this query
        # even if we haven't yielded `num_results` posts yet.
        if after is None:
            break
        else:
            query = build_search_endpoint_query( 
                subreddit_name=subreddit_name,
                query_term=query_term,
                sort_by=sort_by,
                after_fullname=after,
            )

    print(f"Submissions query for {subreddit_name=}, {query_term=} yielded {len(seen_ids)} submissions")
    posts_data = []
    for post_data in itertools.chain.from_iterable(result["data"]["children"] for result in results):
        post_data = post_data["data"]
        post_data["query"] = query_term
        posts_data.append(post_data)
    
    return posts_data


def get_comments(post_id: str, comment_id: Optional[str] = None) -> list[dict[str, object]]:
    query = build_toplevel_comment_endpoint_query(post_id, comment_id)
    print(f"- Query: {query}")
    comments_resp = requests.get(query, headers=HEADERS)
    if comments_resp.status_code != 200:
        raise requests.RequestException(
             f"Didn't get a 200 when attempting OAuth authorization, got response: {comments_resp.content}"
        )
    
    comments_resp_loaded = comments_resp.json()
    assert len(comments_resp_loaded) == 2, (
        f"Expected comment response JSON to have 2 elements but got {len(comments_resp_loaded)} elements"
    )
    _post_json, comments_json = comments_resp_loaded

    # dfs the comments from /comments/<post>[/comment/<comment>] endpoint
    # to see whether we need to further explore
    comments_data = []
    more_children = []

    def dfs_comments(
        comments_json: dict[str, object],
        comments_data: list[dict[str, object]],
        more_children: list[str],
        depth: int = 0
    ) -> None:
        for comment in comments_json["data"]["children"]:
            if comment["kind"] != "more":
                comments_data.append(comment["data"])
                if comment["data"]["replies"] != "":
                    dfs_comments(comment["data"]["replies"], comments_data, more_children, depth+1)
            else:
                # we only expect the "kind"="more" case to appear once
                more_children.extend(comment["data"]["children"])
    
    dfs_comments(comments_json, comments_data, more_children, 0)

    if len(more_children) > 0:
        # there might be more children (extended comments). use this API to get (most of) them
        def dfs_morechildren(comment_ids: list[str], comments_data: list[dict[str, object]], depth: int = 0):
            for i in range(1 + (len(comment_ids) // BATCH_SIZE)):
                curr = BATCH_SIZE * i
                next = BATCH_SIZE * i + BATCH_SIZE
                comment_ids_batch = comment_ids[curr:next]
                query = build_more_children_comment_endpoint_query(post_id, comment_ids_batch)
                print(f"-- Query: {query}")
                more_children_resp = requests.get(query, headers=HEADERS)
                if more_children_resp.status_code != 200:
                    raise requests.RequestException(
                        f"Didn't get a 200 when attempting OAuth authorization, got response: {more_children_resp.content}"
                    )
                things = more_children_resp.json()["json"]["data"]["things"]
                # batch the requests, otherwise we'll end up with a lot of wasted requests
                next_comment_ids = []
                for thing in things:
                    if thing["kind"] == "more" and thing["data"]["count"] > 0:
                        next_comment_ids.extend(thing["data"]["children"])
                    else:
                        comments_data.append(thing["data"])
                        if "replies" in thing["data"] and len(thing["data"]["replies"]) > 0:
                            next_comment_ids.extend(thing["data"]["replies"])

                if len(next_comment_ids) > 0:
                    dfs_morechildren(next_comment_ids, comments_data, depth+1)
                            
        dfs_morechildren(more_children, comments_data)

    print(f"Comments query for {post_id=}, {comment_id=} yielded {len(comments_data)} comments")

    return comments_data