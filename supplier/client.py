"""Supplier HTTP client with robust pagination, rate-limiting, and retry logic."""
import logging
import math
import time
from urllib.parse import urlencode, urljoin
import requests
from django.conf import settings
from core.exceptions import SupplierUnavailableError

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
DEFAULT_TIMEOUT = 15


class SupplierClient:
    """
    HTTP Client for fetching supplier catalog feeds.
    Strictly follows contract §8:
    - Retries up to 5 times per page for 429 and 5xx/connection errors.
    - Honors Retry-After headers on 429.
    - Fast fail on 401.
    - All-or-nothing: fetches entire feed into memory.
    """

    def __init__(self, base_url: str = None, api_key: str = None, session: requests.Session = None):
        self.base_url = (base_url or getattr(settings, "SUPPLIER_API_URL", "http://127.0.0.1:8099")).rstrip("/")
        self.api_key = api_key or getattr(settings, "SUPPLIER_API_KEY", "")
        self.session = session or requests.Session()
        self.requests_made = 0
        self.pages_fetched = 0

    def fetch_all_products(self):
        """
        Fetch all pages until next_cursor is null.
        Returns:
            tuple: (all_items, pages_fetched, requests_made)
        Raises:
            SupplierUnavailableError on 401 or if any page fails after 5 attempts.
        """
        cursor = None
        all_items = []
        self.requests_made = 0
        self.pages_fetched = 0

        while True:
            items, next_cursor = self._fetch_page_with_retries(cursor)
            all_items.extend(items)
            self.pages_fetched += 1

            if not next_cursor:
                break
            cursor = next_cursor

        return all_items, self.pages_fetched, self.requests_made

    def _fetch_page_with_retries(self, cursor: str):
        """
        Fetch a single page with up to 5 attempts.
        """
        params = {"limit": 20}
        if cursor:
            params["cursor"] = cursor

        url = f"{self.base_url}/v1/products?{urlencode(params)}"
        headers = {
            "X-Api-Key": self.api_key,
            "Accept": "application/json",
        }

        last_error = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            self.requests_made += 1
            try:
                response = self.session.get(url, headers=headers, timeout=DEFAULT_TIMEOUT)

                if response.status_code == 200:
                    try:
                        data = response.json()
                        items = data.get("items", [])
                        next_cursor = data.get("next_cursor")
                        return items, next_cursor
                    except Exception as e:
                        last_error = f"Malformed JSON from supplier: {e}"
                        # Retry on parse error like 5xx
                        time.sleep(0.5 * (2 ** (attempt - 1)))
                        continue

                if response.status_code == 401:
                    logger.error("Supplier API key rejected (401)")
                    raise SupplierUnavailableError("Supplier API key was rejected.")

                if response.status_code == 429:
                    retry_after_hdr = response.headers.get("Retry-After")
                    try:
                        wait_sec = float(retry_after_hdr) if retry_after_hdr else 1.0
                    except (ValueError, TypeError):
                        wait_sec = 1.0
                    # Sanity cap to avoid exceeding total run budget
                    wait_sec = max(0.01, min(wait_sec, 10.0))
                    logger.warning("Supplier 429 rate limit hit. Waiting %s seconds...", wait_sec)
                    if attempt < MAX_ATTEMPTS:
                        time.sleep(wait_sec)
                        continue
                    else:
                        last_error = "Rate limit (429) exceeded 5 attempts."
                        break

                if response.status_code in (500, 502, 503, 504):
                    last_error = f"Supplier HTTP {response.status_code}"
                    if attempt < MAX_ATTEMPTS:
                        backoff = min(4.0, 0.5 * (2 ** (attempt - 1)))
                        time.sleep(backoff)
                        continue
                    break

                # Other HTTP status codes (e.g. 400, 403, 404)
                last_error = f"Unexpected supplier status {response.status_code}: {response.text[:200]}"
                break

            except (requests.ConnectionError, requests.Timeout, requests.RequestException) as e:
                last_error = f"Network connection error: {e}"
                if attempt < MAX_ATTEMPTS:
                    backoff = min(4.0, 0.5 * (2 ** (attempt - 1)))
                    time.sleep(backoff)
                    continue
                break

        raise SupplierUnavailableError(f"Failed to fetch supplier page after {MAX_ATTEMPTS} attempts: {last_error}")
