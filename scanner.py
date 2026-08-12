import asyncio
import hashlib
import logging
from typing import Any

import aiohttp


logger = logging.getLogger(__name__)


GITHUB_API = "https://api.github.com"


class GitHubScanner:

    def __init__(
        self,
        token: str = "",
    ):

        self.token = token.strip()

    # =====================================================
    # HEADERS
    # =====================================================

    def headers(self) -> dict:

        headers = {
            "Accept": (
                "application/vnd.github+json"
            ),
            "User-Agent": (
                "Private-GitHub-Secret-Monitor"
            ),
            "X-GitHub-Api-Version": (
                "2022-11-28"
            ),
        }

        if self.token:

            headers["Authorization"] = (
                f"Bearer {self.token}"
            )

        return headers

    # =====================================================
    # FINGERPRINT
    # =====================================================

    @staticmethod
    def fingerprint(
        value: str,
    ) -> str:

        digest = hashlib.sha256(
            value.encode(
                "utf-8",
                errors="ignore",
            )
        ).hexdigest()

        return digest[:16]

    # =====================================================
    # SEARCH
    # =====================================================

    async def search(
        self,
        session: aiohttp.ClientSession,
        query: str,
        provider: str,
        limit: int = 30,
    ) -> list[dict[str, Any]]:

        url = (
            f"{GITHUB_API}/search/code"
        )

        params = {
            "q": query,
            "per_page": min(
                limit,
                100,
            ),
        }

        try:

            async with session.get(
                url,
                params=params,
                headers=self.headers(),
            ) as response:

                # -----------------------------------------
                # RATE LIMIT
                # -----------------------------------------

                if response.status == 403:

                    remaining = response.headers.get(
                        "X-RateLimit-Remaining",
                        "?",
                    )

                    reset = response.headers.get(
                        "X-RateLimit-Reset",
                        "?",
                    )

                    logger.warning(
                        "GitHub rate limit. "
                        "Remaining=%s Reset=%s",
                        remaining,
                        reset,
                    )

                    return []

                # -----------------------------------------
                # NOT FOUND / UNAUTHORIZED
                # -----------------------------------------

                if response.status == 401:

                    logger.error(
                        "GitHub token rejected."
                    )

                    return []

                # -----------------------------------------
                # OTHER ERRORS
                # -----------------------------------------

                if response.status != 200:

                    body = await response.text()

                    logger.warning(
                        "GitHub API returned %s: %s",
                        response.status,
                        body[:300],
                    )

                    return []

                data = await response.json()

                items = data.get(
                    "items",
                    [],
                )

                results = []

                for item in items:

                    repository = (
                        item.get(
                            "repository",
                            {},
                        )
                        .get(
                            "full_name",
                            "unknown",
                        )
                    )

                    path = item.get(
                        "path",
                        "unknown",
                    )

                    url = item.get(
                        "html_url",
                        "",
                    )

                    # The API result itself does not expose
                    # the secret value here.
                    results.append(
                        {
                            "provider": provider,
                            "repository": repository,
                            "path": path,
                            "url": url,
                            "fingerprint": (
                                self.fingerprint(
                                    f"{provider}|"
                                    f"{repository}|"
                                    f"{path}"
                                )
                            ),
                        }
                    )

                return results

        except asyncio.TimeoutError:

            logger.warning(
                "GitHub request timed out."
            )

            return []

        except aiohttp.ClientError as error:

            logger.warning(
                "GitHub HTTP error: %s",
                error,
            )

            return []

        except Exception:

            logger.exception(
                "Unexpected GitHub scanner error."
            )

            return []

    # =====================================================
    # MULTI QUERY SCAN
    # =====================================================

    async def scan_queries(
        self,
        queries: list[dict[str, str]],
        limit: int = 30,
    ) -> list[dict[str, Any]]:

        results = []

        timeout = aiohttp.ClientTimeout(
            total=30
        )

        connector = aiohttp.TCPConnector(
            limit=4,
            ttl_dns_cache=300,
        )

        async with aiohttp.ClientSession(
            timeout=timeout,
            connector=connector,
        ) as session:

            for item in queries:

                provider = item[
                    "provider"
                ]

                query = item[
                    "query"
                ]

                logger.info(
                    "Searching GitHub: %s (%s)",
                    provider,
                    query,
                )

                found = await self.search(
                    session=session,
                    query=query,
                    provider=provider,
                    limit=limit,
                )

                results.extend(
                    found
                )

                # Avoid hammering GitHub.
                await asyncio.sleep(
                    1.0
                )

        # =================================================
        # DEDUPLICATE
        # =================================================

        unique = {}

        for item in results:

            key = (
                f"{item['provider']}|"
                f"{item['repository']}|"
                f"{item['path']}"
            )

            unique[key] = item

        return list(
            unique.values()
        )
