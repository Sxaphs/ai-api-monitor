import httpx
import pytest

from app.collectors.http import FetchError, Fetcher
from app.collectors.twitter import TwitterClient
from app.config import Settings


def config():
    return Settings(
        database_url="sqlite://", rapidapi_key="test-key", scheduler_enabled=False,
        log_level="INFO", request_timeout=30, retry_attempts=3, retry_backoff=0,
    )


def test_timeline_passes_screenname_and_parses_tweets():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"timeline": [{"tweet_id": "1", "text": "hello"}]})

    fetcher = Fetcher(config(), sleep=lambda _: None, transport=httpx.MockTransport(handler))
    tweets = TwitterClient(fetcher, config()).timeline("@OpenRouter")
    assert tweets == [{"tweet_id": "1", "text": "hello"}]
    assert requests[0].url.params["screenname"] == "OpenRouter"
    assert requests[0].headers["x-rapidapi-key"] == "test-key"


def test_timeline_empty_response_is_an_explicit_error():
    fetcher = Fetcher(
        config(), sleep=lambda _: None,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="")),
    )
    with pytest.raises(FetchError, match="空响应"):
        TwitterClient(fetcher, config()).timeline("OpenRouterAI")
