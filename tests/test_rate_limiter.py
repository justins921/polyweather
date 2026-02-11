"""Tests for token-bucket rate limiter."""

import asyncio
import time

import pytest

from clients.kalshi_svc import TokenBucket


@pytest.mark.asyncio
async def test_burst_capacity():
    """Should allow burst up to rate tokens immediately."""
    bucket = TokenBucket(rate=5)
    start = time.monotonic()
    for _ in range(5):
        await bucket.acquire()
    elapsed = time.monotonic() - start
    # All 5 should complete nearly instantly (burst)
    assert elapsed < 0.2


@pytest.mark.asyncio
async def test_rate_limiting_kicks_in():
    """After burst, additional tokens should be delayed."""
    bucket = TokenBucket(rate=10)
    # Drain the bucket
    for _ in range(10):
        await bucket.acquire()
    # Next acquire should take ~0.1s (1/10)
    start = time.monotonic()
    await bucket.acquire()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.05  # some delay expected


@pytest.mark.asyncio
async def test_tokens_refill():
    """Tokens should refill over time."""
    bucket = TokenBucket(rate=10)
    # Drain
    for _ in range(10):
        await bucket.acquire()
    # Wait for refill
    await asyncio.sleep(0.5)
    # Should have ~5 tokens now
    start = time.monotonic()
    for _ in range(4):
        await bucket.acquire()
    elapsed = time.monotonic() - start
    assert elapsed < 0.2  # should be fast since tokens refilled


@pytest.mark.asyncio
async def test_single_token():
    """Rate=1 should allow 1 per second."""
    bucket = TokenBucket(rate=1)
    await bucket.acquire()  # uses the 1 token
    start = time.monotonic()
    await bucket.acquire()  # must wait ~1s
    elapsed = time.monotonic() - start
    assert elapsed >= 0.5  # at least partial delay
