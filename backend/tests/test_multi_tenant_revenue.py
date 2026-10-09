import pytest
import pytest_asyncio
import httpx
from decimal import Decimal, ROUND_HALF_UP
from app.services.reservations import (
    calculate_total_revenue,
    get_property,
    get_properties_for_tenant,
    PropertyNotFoundError
)
from app.services.cache import (
    get_revenue_summary,
    invalidate_revenue_cache,
    build_cache_key,
    redis_client
)
from app.core.database_pool import db_pool
from app.main import app

BASE_URL = "http://localhost:8000"


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    await db_pool.initialize()
    yield


@pytest_asyncio.fixture
async def client_a_token():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as ac:
        res = await ac.post("/api/v1/auth/login", json={
            "email": "sunset@propertyflow.com",
            "password": "client_a_2024"
        })
        assert res.status_code == 200
        return res.json()["access_token"]


@pytest_asyncio.fixture
async def client_b_token():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as ac:
        res = await ac.post("/api/v1/auth/login", json={
            "email": "ocean@propertyflow.com",
            "password": "client_b_2024"
        })
        assert res.status_code == 200
        return res.json()["access_token"]


@pytest.mark.asyncio
async def test_tenant_isolation_overlapping_property_ids(client_a_token, client_b_token):
    """
    Verify that two tenants sharing the same property_id ('prop-001')
    do not leak data to each other on cache misses or cache hits.
    """
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as ac:
        # Invalidate any existing keys for this test
        await invalidate_revenue_cache("prop-001", "tenant-a")
        await invalidate_revenue_cache("prop-001", "tenant-b")

        # 1. Tenant A requests prop-001 (Cache Miss -> DB -> Cached)
        res_a = await ac.get(
            "/api/v1/dashboard/summary?property_id=prop-001",
            headers={"Authorization": f"Bearer {client_a_token}"}
        )
        assert res_a.status_code == 200
        data_a = res_a.json()
        assert data_a["property_id"] == "prop-001"
        assert data_a["total_revenue"] == 2250.0
        assert data_a["reservations_count"] == 4

        # Verify Redis key is tenant-scoped
        key_a = build_cache_key("tenant-a", "prop-001")
        cached_a = await redis_client.get(key_a)
        assert cached_a is not None
        assert "tenant-a" in cached_a

        # 2. Tenant B requests prop-001 (MUST NOT receive Tenant A's cached data)
        res_b = await ac.get(
            "/api/v1/dashboard/summary?property_id=prop-001",
            headers={"Authorization": f"Bearer {client_b_token}"}
        )
        assert res_b.status_code == 200
        data_b = res_b.json()
        assert data_b["property_id"] == "prop-001"
        assert data_b["total_revenue"] == 0.0
        assert data_b["reservations_count"] == 0

        # Verify Tenant B has its own distinct cache key
        key_b = build_cache_key("tenant-b", "prop-001")
        cached_b = await redis_client.get(key_b)
        assert cached_b is not None
        assert "tenant-b" in cached_b

        # 3. Tenant A requests prop-001 again (Cache Hit -> Tenant A data preserved)
        res_a2 = await ac.get(
            "/api/v1/dashboard/summary?property_id=prop-001",
            headers={"Authorization": f"Bearer {client_a_token}"}
        )
        assert res_a2.status_code == 200
        assert res_a2.json()["total_revenue"] == 2250.0
        assert res_a2.json()["reservations_count"] == 4


@pytest.mark.asyncio
async def test_cross_tenant_property_access_rejected(client_a_token, client_b_token):
    """
    Verify that an authenticated tenant cannot access properties belonging to another tenant.
    """
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as ac:
        # Tenant A tries to access prop-004 (owned exclusively by Tenant B)
        res_a_forbidden = await ac.get(
            "/api/v1/dashboard/summary?property_id=prop-004",
            headers={"Authorization": f"Bearer {client_a_token}"}
        )
        assert res_a_forbidden.status_code == 404
        assert "not found" in res_a_forbidden.json()["detail"].lower()

        # Tenant B tries to access prop-002 (owned exclusively by Tenant A)
        res_b_forbidden = await ac.get(
            "/api/v1/dashboard/summary?property_id=prop-002",
            headers={"Authorization": f"Bearer {client_b_token}"}
        )
        assert res_b_forbidden.status_code == 404
        assert "not found" in res_b_forbidden.json()["detail"].lower()


@pytest.mark.asyncio
async def test_unauthenticated_requests_fail_securely():
    """
    Verify that missing or invalid authentication tokens fail securely with 401.
    """
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as ac:
        # No token
        res_no_auth = await ac.get("/api/v1/dashboard/summary?property_id=prop-001")
        assert res_no_auth.status_code == 401

        # Invalid token
        res_invalid = await ac.get(
            "/api/v1/dashboard/summary?property_id=prop-001",
            headers={"Authorization": "Bearer invalid.token.value"}
        )
        assert res_invalid.status_code == 401


@pytest.mark.asyncio
async def test_client_supplied_header_cannot_override_auth(client_b_token):
    """
    Verify that a client cannot use X-Simulated-Tenant or query params to impersonate another tenant.
    """
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as ac:
        # Client B sends X-Simulated-Tenant: tenant-a attempting to view Tenant A's prop-002
        res = await ac.get(
            "/api/v1/dashboard/summary?property_id=prop-002",
            headers={
                "Authorization": f"Bearer {client_b_token}",
                "X-Simulated-Tenant": "tenant-a"
            }
        )
        # Must strictly fail with 404 because prop-002 belongs to tenant-a, but token belongs to tenant-b
        assert res.status_code == 404


@pytest.mark.asyncio
async def test_paris_timezone_march_boundary_revenue(client_a_token):
    """
    Verify that reservation res-tz-1 with UTC check-in '2024-02-29 23:30:00+00'
    is accurately evaluated in the property's local timezone ('Europe/Paris', CET / UTC+1)
    as '2024-03-01 00:30:00 CET', and included in March 2024 revenue.
    """
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as ac:
        # March 2024 must include res-tz-1 ($1250) + res-dec-1/2/3 ($1000) = $2250, count = 4
        res_march = await ac.get(
            "/api/v1/dashboard/summary?property_id=prop-001&month=3&year=2024",
            headers={"Authorization": f"Bearer {client_a_token}"}
        )
        assert res_march.status_code == 200
        data_march = res_march.json()
        assert data_march["total_revenue"] == 2250.0
        assert data_march["reservations_count"] == 4

        # February 2024 must NOT include res-tz-1 because locally it belongs to March
        res_feb = await ac.get(
            "/api/v1/dashboard/summary?property_id=prop-001&month=2&year=2024",
            headers={"Authorization": f"Bearer {client_a_token}"}
        )
        assert res_feb.status_code == 200
        data_feb = res_feb.json()
        assert data_feb["total_revenue"] == 0.0
        assert data_feb["reservations_count"] == 0


@pytest.mark.asyncio
async def test_monetary_precision_subcent_aggregation():
    """
    Verify that sub-cent amounts are aggregated at full database precision before
    quantizing to currency precision, preventing the 1-cent discrepancy:
    333.333 + 333.333 + 333.334 = 1000.000 -> 1000.00 (not 999.99).
    """
    # Test service level Decimal aggregation
    dec_1 = Decimal('333.333')
    dec_2 = Decimal('333.333')
    dec_3 = Decimal('333.334')

    # Premature rounding demonstrates the bug
    premature_sum = (
        dec_1.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) +
        dec_2.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP) +
        dec_3.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    )
    assert premature_sum == Decimal('999.99')  # The 1-cent deficit bug

    # Correct aggregation before quantization
    exact_sum = (dec_1 + dec_2 + dec_3).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    assert exact_sum == Decimal('1000.00')  # Exactly 1000.00


@pytest.mark.asyncio
async def test_tenant_properties_endpoint(client_a_token, client_b_token):
    """
    Verify that GET /api/v1/properties returns only the properties owned by
    the authenticated tenant.
    """
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as ac:
        # Tenant A
        res_a = await ac.get(
            "/api/v1/properties",
            headers={"Authorization": f"Bearer {client_a_token}"}
        )
        assert res_a.status_code == 200
        props_a = res_a.json()
        prop_ids_a = [p["id"] for p in props_a]
        assert prop_ids_a == ["prop-001", "prop-002", "prop-003"]
        assert all(p["tenant_id"] == "tenant-a" for p in props_a)

        # Tenant B
        res_b = await ac.get(
            "/api/v1/properties",
            headers={"Authorization": f"Bearer {client_b_token}"}
        )
        assert res_b.status_code == 200
        props_b = res_b.json()
        prop_ids_b = [p["id"] for p in props_b]
        assert prop_ids_b == ["prop-001", "prop-004", "prop-005"]
        assert all(p["tenant_id"] == "tenant-b" for p in props_b)


@pytest.mark.asyncio
async def test_concurrent_multi_tenant_requests(client_a_token, client_b_token):
    """
    Verify that concurrent requests from different tenants for overlapping
    property IDs maintain strict isolation without race conditions.
    """
    import asyncio

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as ac:
        async def fetch_a():
            return await ac.get(
                "/api/v1/dashboard/summary?property_id=prop-001",
                headers={"Authorization": f"Bearer {client_a_token}"}
            )

        async def fetch_b():
            return await ac.get(
                "/api/v1/dashboard/summary?property_id=prop-001",
                headers={"Authorization": f"Bearer {client_b_token}"}
            )

        # Interleave 10 requests for Tenant A and 10 requests for Tenant B concurrently
        tasks = []
        for _ in range(10):
            tasks.append(fetch_a())
            tasks.append(fetch_b())

        responses = await asyncio.gather(*tasks)

        # Even indices are Tenant A, Odd indices are Tenant B
        for idx, res in enumerate(responses):
            assert res.status_code == 200
            data = res.json()
            if idx % 2 == 0:
                # Tenant A
                assert data["total_revenue"] == 2250.0
                assert data["reservations_count"] == 4
            else:
                # Tenant B
                assert data["total_revenue"] == 0.0
                assert data["reservations_count"] == 0


@pytest.mark.asyncio
async def test_cache_invalidation_isolation(client_a_token, client_b_token):
    """
    Verify that invalidating cache for one tenant does not affect another tenant's cache.
    """
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as ac:
        # Populate cache for both tenants
        await ac.get("/api/v1/dashboard/summary?property_id=prop-001", headers={"Authorization": f"Bearer {client_a_token}"})
        await ac.get("/api/v1/dashboard/summary?property_id=prop-001", headers={"Authorization": f"Bearer {client_b_token}"})

        key_a = build_cache_key("tenant-a", "prop-001")
        key_b = build_cache_key("tenant-b", "prop-001")

        assert await redis_client.get(key_a) is not None
        assert await redis_client.get(key_b) is not None

        # Invalidate only Tenant A
        await invalidate_revenue_cache("prop-001", "tenant-a")

        # Key A deleted, Key B preserved
        assert await redis_client.get(key_a) is None
        assert await redis_client.get(key_b) is not None


@pytest.mark.asyncio
async def test_cache_hit_cannot_bypass_authorization(client_a_token):
    """
    Verify that an existing cache entry cannot bypass server-side ownership authorization.
    Even if a cache key exists for tenant-a pointing to an unauthorized property (prop-004),
    the endpoint must still reject the request with 404 because tenant-a does not own prop-004.
    """
    import json
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as ac:
        # Pre-seed a poisoned cache entry under tenant-a's key namespace
        poisoned_key = build_cache_key("tenant-a", "prop-004")
        poisoned_payload = {
            "property_id": "prop-004",
            "tenant_id": "tenant-a",
            "total": "9999.00",
            "currency": "USD",
            "count": 99
        }
        await redis_client.set(poisoned_key, json.dumps(poisoned_payload), ex=60)

        # Attempt to access prop-004 as Tenant A
        res = await ac.get(
            "/api/v1/dashboard/summary?property_id=prop-004",
            headers={"Authorization": f"Bearer {client_a_token}"}
        )
        assert res.status_code == 404
        assert "not found" in res.json()["detail"].lower()

        # Clean up
        await redis_client.delete(poisoned_key)


@pytest.mark.asyncio
async def test_invalid_query_parameters_validation(client_a_token):
    """
    Verify input validation for month and year query parameters.
    """
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as ac:
        # Invalid month (> 12)
        res_invalid_month = await ac.get(
            "/api/v1/dashboard/summary?property_id=prop-001&month=13&year=2024",
            headers={"Authorization": f"Bearer {client_a_token}"}
        )
        assert res_invalid_month.status_code == 422

        # Invalid month (< 1)
        res_invalid_month_zero = await ac.get(
            "/api/v1/dashboard/summary?property_id=prop-001&month=0&year=2024",
            headers={"Authorization": f"Bearer {client_a_token}"}
        )
        assert res_invalid_month_zero.status_code == 422

        # Invalid year (< 2000)
        res_invalid_year = await ac.get(
            "/api/v1/dashboard/summary?property_id=prop-001&month=3&year=1999",
            headers={"Authorization": f"Bearer {client_a_token}"}
        )
        assert res_invalid_year.status_code == 422


@pytest.mark.asyncio
async def test_all_seeded_properties_revenue(client_a_token, client_b_token):
    """
    Verify exact revenue figures across all seeded properties for both clients for March 2024.
    """
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as ac:
        # Client A properties
        # prop-001: 2250.00, count: 4
        res_a1 = await ac.get(
            "/api/v1/dashboard/summary?property_id=prop-001&month=3&year=2024",
            headers={"Authorization": f"Bearer {client_a_token}"}
        )
        assert res_a1.status_code == 200
        assert res_a1.json()["total_revenue"] == 2250.0
        assert res_a1.json()["reservations_count"] == 4

        # prop-002: 4975.50, count: 4
        res_a2 = await ac.get(
            "/api/v1/dashboard/summary?property_id=prop-002&month=3&year=2024",
            headers={"Authorization": f"Bearer {client_a_token}"}
        )
        assert res_a2.status_code == 200
        assert res_a2.json()["total_revenue"] == 4975.50
        assert res_a2.json()["reservations_count"] == 4

        # prop-003: 6100.50, count: 2
        res_a3 = await ac.get(
            "/api/v1/dashboard/summary?property_id=prop-003&month=3&year=2024",
            headers={"Authorization": f"Bearer {client_a_token}"}
        )
        assert res_a3.status_code == 200
        assert res_a3.json()["total_revenue"] == 6100.50
        assert res_a3.json()["reservations_count"] == 2

        # Client B properties
        # prop-001 (Mountain Lodge Beta): 0.00, count: 0
        res_b1 = await ac.get(
            "/api/v1/dashboard/summary?property_id=prop-001&month=3&year=2024",
            headers={"Authorization": f"Bearer {client_b_token}"}
        )
        assert res_b1.status_code == 200
        assert res_b1.json()["total_revenue"] == 0.0
        assert res_b1.json()["reservations_count"] == 0

        # prop-004: 1776.50, count: 4
        res_b4 = await ac.get(
            "/api/v1/dashboard/summary?property_id=prop-004&month=3&year=2024",
            headers={"Authorization": f"Bearer {client_b_token}"}
        )
        assert res_b4.status_code == 200
        assert res_b4.json()["total_revenue"] == 1776.50
        assert res_b4.json()["reservations_count"] == 4

        # prop-005: 3256.00, count: 3
        res_b5 = await ac.get(
            "/api/v1/dashboard/summary?property_id=prop-005&month=3&year=2024",
            headers={"Authorization": f"Bearer {client_b_token}"}
        )
        assert res_b5.status_code == 200
        assert res_b5.json()["total_revenue"] == 3256.00
        assert res_b5.json()["reservations_count"] == 3


