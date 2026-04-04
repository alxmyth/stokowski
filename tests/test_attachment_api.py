"""Tests for Linear attachment API methods."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from stokowski.linear import LinearClient


@pytest.fixture
def client(monkeypatch):
    # Clear proxy env vars so httpx doesn't try to use a SOCKS proxy
    for var in ("ALL_PROXY", "HTTPS_PROXY", "HTTP_PROXY",
                "all_proxy", "https_proxy", "http_proxy"):
        monkeypatch.delenv(var, raising=False)
    return LinearClient(
        endpoint="https://api.linear.app/graphql",
        api_key="test-key",
    )


class TestUpsertAttachment:
    @pytest.mark.asyncio
    async def test_upsert_sends_correct_mutation(self, client):
        client._graphql = AsyncMock(return_value={
            "attachmentCreate": {"success": True, "attachment": {"id": "att-1"}}
        })
        result = await client.upsert_stokowski_attachment(
            issue_id="issue-1",
            identifier="ALX-9",
            metadata={"state": "implement", "run": 1},
            subtitle="implement · run 1",
        )
        assert result is True
        call_args = client._graphql.call_args
        variables = call_args[0][1]
        assert variables["issueId"] == "issue-1"
        assert variables["url"] == "stokowski://state/ALX-9"
        assert variables["title"] == "Stokowski"
        assert variables["subtitle"] == "implement · run 1"
        assert variables["metadata"] == {"state": "implement", "run": 1}

    @pytest.mark.asyncio
    async def test_upsert_returns_false_on_error(self, client):
        client._graphql = AsyncMock(side_effect=RuntimeError("API error"))
        result = await client.upsert_stokowski_attachment(
            issue_id="issue-1", identifier="ALX-9",
            metadata={}, subtitle="",
        )
        assert result is False


class TestFetchAttachment:
    @pytest.mark.asyncio
    async def test_fetch_returns_metadata(self, client):
        client._graphql = AsyncMock(return_value={
            "attachmentsForURL": {
                "nodes": [{"id": "att-1", "metadata": {"state": "implement", "run": 2}}]
            }
        })
        result = await client.fetch_stokowski_attachment("ALX-9")
        assert result == {"state": "implement", "run": 2}

    @pytest.mark.asyncio
    async def test_fetch_returns_none_when_missing(self, client):
        client._graphql = AsyncMock(return_value={
            "attachmentsForURL": {"nodes": []}
        })
        result = await client.fetch_stokowski_attachment("ALX-9")
        assert result is None

    @pytest.mark.asyncio
    async def test_fetch_returns_none_on_error(self, client):
        client._graphql = AsyncMock(side_effect=RuntimeError("API error"))
        result = await client.fetch_stokowski_attachment("ALX-9")
        assert result is None


class TestDeleteAttachment:
    @pytest.mark.asyncio
    async def test_delete_finds_and_removes(self, client):
        client._graphql = AsyncMock(side_effect=[
            {"attachmentsForURL": {"nodes": [{"id": "att-1"}]}},
            {"attachmentDelete": {"success": True}},
        ])
        result = await client.delete_stokowski_attachment("ALX-9")
        assert result is True
        assert client._graphql.call_count == 2

    @pytest.mark.asyncio
    async def test_delete_returns_false_when_not_found(self, client):
        client._graphql = AsyncMock(return_value={
            "attachmentsForURL": {"nodes": []}
        })
        result = await client.delete_stokowski_attachment("ALX-9")
        assert result is False
