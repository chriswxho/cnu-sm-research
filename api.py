from collections import deque
from enum import Enum
import itertools
import json
import os
import secrets
import time
from typing import Optional

import requests
import requests.auth as auth


USER_AGENT = "CNU Social Media Research Client"
REDDIT_ACCESS_TOKEN_URL: str = "https://www.reddit.com/api/v1/access_token"
REDDIT_OAUTH_BASE_URL: str = "https://oauth.reddit.com"
REQUESTS_MANAGER_LOG_PREFIX: str = "[RequestManager]"

# only for /api/morechildren
BATCH_SIZE: int = 100

class SortBy(Enum):
    RELEVANCE = "relevance"
    HOT = "hot"
    TOP = "top"
    NEW = "new"
    COMMENTS = "comments"


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


class RedditRequestManager:
    def __init__(self, window_time_sec: int, max_requests_in_window: int) -> None:
        self._just_started = True
        self._state = secrets.token_urlsafe(30)
        if window_time_sec > 10 * 60:
            print(
                f"{REQUESTS_MANAGER_LOG_PREFIX} window_time_sec needs to be <= 10 min, flooring to 600."
            )
            window_time_sec = 600
        self._window_time = window_time_sec
        if max_requests_in_window > 1000:
            print(
                f"{REQUESTS_MANAGER_LOG_PREFIX} max_requests_in_window needs to be <= 1000, flooring to 1000."
            )
            max_requests_in_window = 1000
        self._max_requests_in_window = max_requests_in_window

        # front of queue = oldest query
        self._request_unixtime_queue = deque(maxlen=max_requests_in_window)

        if not os.path.exists("keys.json"):
            raise FileNotFoundError(
                f"File `keys.json` was not found in the current working directory {os.getcwd()}. "
                "Please ensure the `keys.json` file is placed in the same parent folder (should be `cnu-sm-research/`)."
            )
        with open("keys.json", "rb") as keys_file:
            reddit_api_keys = json.load(keys_file)
            self._client_id = reddit_api_keys["CLIENT_ID"]
            self._secret_id = reddit_api_keys["SECRET_ID"]
        self._access_token = self._get_access_token()

        self._headers = {
            "User-Agent": USER_AGENT,
            "Authorization": f"bearer {self._access_token}",
        }


    def _request_get(self, query) -> requests.Response:
        oldest_request_unixtime = (
            self._request_unixtime_queue[0] if len(self._request_unixtime_queue) else -1
        )
        now = int(time.monotonic())
        while (
            len(self._request_unixtime_queue) == self._max_requests_in_window 
            and now - oldest_request_unixtime < self._window_time
        ):
            print(
                f"{REQUESTS_MANAGER_LOG_PREFIX} Request window with window_time={self._window_time}s full, "
                f"waiting ~{self._window_time - (now - oldest_request_unixtime)} more seconds"
            )
            time.sleep(10)
            now = int(time.monotonic())
        
        while (
            len(self._request_unixtime_queue) > 0
            and now - oldest_request_unixtime > self._window_time
        ):
            self._request_unixtime_queue.popleft()
        
        now = int(time.monotonic())
        self._request_unixtime_queue.append(now)
        print(f"{REQUESTS_MANAGER_LOG_PREFIX} sent query: {query}")
        resp = requests.get(query, headers=self._headers)
        if resp.status_code != 200:
            raise requests.RequestException(
                f"Didn't get a 200 for this query, got response: {resp.content}",
            )
        if "X-Ratelimit-Used" not in resp.headers:
            print(f"{REQUESTS_MANAGER_LOG_PREFIX} No ratelimit info available for this request. {resp.headers=}")
        else:
            requests_used = int(resp.headers["X-Ratelimit-Used"])
            if self._just_started and requests_used > 1:  # not including the request we just sent
                time_until_window_reset = int(resp.headers["X-Ratelimit-Reset"])
                requests_remaining = int(float(resp.headers["X-Ratelimit-Remaining"]))
                print(
                    f"{REQUESTS_MANAGER_LOG_PREFIX} catching up on metadata, "
                    f"{requests_used=}, {requests_remaining=}, {time_until_window_reset=}"
                )
                if self._window_time < time_until_window_reset:
                    time_until_window_reset = self._window_time
                
                # we need to track pre-existing queries, as logged by the response headers
                for _ in range(requests_used - 1):
                    self._request_unixtime_queue.appendleft(now - time_until_window_reset)
                self._just_started = False
        
        return resp


    def _get_access_token(self) -> str:
        client_auth = auth.HTTPBasicAuth(self._client_id, self._secret_id)
        post_data = {
            "grant_type": "client_credentials",
            "device_id": self._state,
        }
        headers = {"User-Agent": USER_AGENT}
        auth_response = requests.post(
            REDDIT_ACCESS_TOKEN_URL, 
            auth=client_auth,
            data=post_data,
            headers=headers,
        )

        if auth_response.status_code != 200:
            raise requests.RequestException(
                f"Didn't get a 200 when attempting OAuth authorization, got response: {auth_response.content}"
            )
        access_token = auth_response.json()["access_token"]
        return access_token


    def search_posts(
        self,
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
            search_resp = self._request_get(query)
            search_json = search_resp.json()
            return search_json
        
        results = []
        seen_ids = set()
        while len(seen_ids) < num_results:
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

        print(
            f"{REQUESTS_MANAGER_LOG_PREFIX} Submissions query for {subreddit_name=}, {query_term=} "
            f"yielded {len(seen_ids)} submissions"
        )
        posts_data = []
        for post_data in itertools.chain.from_iterable(result["data"]["children"] for result in results):
            post_data = post_data["data"]
            post_data["query"] = query_term
            posts_data.append(post_data)
        
        return posts_data


    def get_comments(self, post_id: str, comment_id: Optional[str] = None) -> list[dict[str, object]]:
        query = build_toplevel_comment_endpoint_query(post_id, comment_id)
        comments_resp = self._request_get(query)
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
                    more_children_resp = self._request_get(query)
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

        print(
            f"{REQUESTS_MANAGER_LOG_PREFIX} Comments query for {post_id=}, {comment_id=} yielded {len(comments_data)} comments"
        )

        return comments_data