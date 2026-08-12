import asyncio
import hashlib
import re
from typing import Any

import aiohttp


GITHUB_API = "https://api.github.com"

PATTERNS = {
    "xAI": re.compile(
        r"\bxai-[A-Za-z0-9_-]{20,}\b"
    ),

    "OpenAI": re.compile(
        r"\bsk-[A-Za-z0-9_-]{20,}\b"
    ),

    "Groq": re.compile(
        r"\bgsk_[A-Za-z0-9_-]{20,}\b"
    ),

    "Google": re.compile(
        r"\bAIza[A-Za-z0-9_-]{30,}\b"
    ),

    "Anthropic": re.compile(
        r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"
    ),

    "GitHub": re.compile(
        r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"
    ),
}


class GitHubScanner:
    def __init__(self, token: str = ""):
        self.token = token

    def headers(self) -> dict:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "Private-GitHub-Secret-Monitor",
        }

        if self.token:
            headers["Authorization"] = (
                f"Bearer {self.token}"
            )

        return headers

    @staticmethod
    def fingerprint(value: str) -> str:
        digest = hashlib.sha256(
            value.encode("utf-8", errors="ignore")
        ).hexdigest()

        return digest[:16]

    def detect(self, text: str) -> list[dict[str, str]]:
        findings = []

        for platform, pattern in PATTERNS.items():
            for match in pattern.finditer(text):
                secret = match.group(0)

                findings.append({
                    "platform": platform,
                    "fingerprint": self.fingerprint(secret),
                })

        return findings

    async def search(
        self,
        session: aiohttp.ClientSession,
        query: str,
        limit: int = 30,
    ) -> list[dict[str, Any]]:

        url = f"{GITHUB_API}/search/code"

        params = {
            "q": query,
            "per_page": min(limit, 100),
        }

        try:
            async with session.get(
                url,
                params=params,
                headers=self.headers(),
                timeout=aiohttp.ClientTimeout(total=20),
            ) as response:

                if response.status != 200:
                    return []

                data = await response.json()

                return data.get("items", [])

        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
        ):
            return []

    async def scan_queries(
        self,
        queries: list[str],
        limit: int = 30,
    ) -> list[dict[str, Any]]:

        results = []

        timeout = aiohttp.ClientTimeout(total=30)

        async with aiohttp.ClientSession(
            timeout=timeout
        ) as session:

            for query in queries:
                items = await self.search(
                    session,
                    query,
                    limit,
                )

                for item in items:
                    results.append({
                        "query": query,
                        "repository": item.get(
                            "repository", {}
                        ).get("full_name", "unknown"),

                        "path": item.get(
                            "path", "unknown"
                        ),

                        "url": item.get(
                            "html_url", ""
                        ),
                    })

        return results
