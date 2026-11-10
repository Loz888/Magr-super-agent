"""Regression coverage for H00 boundary checks; no network or database used."""

import asyncio

import pytest

from mgar_v3.domain import DomainError
from mgar_v3.repository_analysis.tools import RepositoryAnalysisTools
from mgar_v3.settings import Settings


@pytest.mark.parametrize("items", [None, {}, "not-a-list"])
def test_malformed_branch_page_returns_domain_error(monkeypatch, items):
    settings = Settings(
        database_url="postgresql://unused:unused@127.0.0.1/unused",
        local_token="controlled-test-token-with-no-external-authority",
    )
    tools = RepositoryAnalysisTools(settings, "octo/example", "a" * 40)

    async def response(*args):
        return [], {"items": items}, []

    monkeypatch.setattr(tools, "_call", response)
    with pytest.raises(DomainError) as captured:
        asyncio.run(tools._branches("main"))
    assert captured.value.code == "MCP_RESPONSE_UNSUPPORTED"


@pytest.mark.parametrize("wrapped", [False, True])
def test_valid_branch_pages_keep_exact_matching_ref(monkeypatch, wrapped):
    settings = Settings(
        database_url="postgresql://unused:unused@127.0.0.1/unused",
        local_token="controlled-test-token-with-no-external-authority",
    )
    tools = RepositoryAnalysisTools(settings, "octo/example", "a" * 40)
    expected = {"name": "main", "commit": {"sha": "b" * 40}}
    items = [None, expected, {"name": "other", "commit": {"sha": "c" * 40}}]

    async def response(*args):
        return [], {"items": items} if wrapped else items, []

    monkeypatch.setattr(tools, "_call", response)
    assert asyncio.run(tools._branches("main")) == [expected]
