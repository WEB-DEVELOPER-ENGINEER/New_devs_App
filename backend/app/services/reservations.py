from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Any, List, Optional
import logging
from sqlalchemy import text
from app.core.database_pool import db_pool

logger = logging.getLogger(__name__)


class PropertyNotFoundError(Exception):
    """Raised when a property is not found or does not belong to the tenant."""
    pass


async def get_property(property_id: str, tenant_id: str) -> Optional[Dict[str, Any]]:
    """
    Fetch property details ensuring tenant isolation.
    Returns None if property does not exist for this tenant.
    """
    await db_pool.initialize()
    async with db_pool.get_session() as session:
        query = text("""
            SELECT id, tenant_id, name, timezone, created_at
            FROM properties
            WHERE id = :property_id AND tenant_id = :tenant_id
        """)
        result = await session.execute(query, {
            "property_id": property_id,
            "tenant_id": tenant_id
        })
        row = result.fetchone()
        if not row:
            return None
        return {
            "id": row.id,
            "tenant_id": row.tenant_id,
            "name": row.name,
            "timezone": row.timezone,
            "created_at": row.created_at.isoformat() if row.created_at else None
        }


async def get_properties_for_tenant(tenant_id: str) -> List[Dict[str, Any]]:
    """
    Retrieve all properties belonging strictly to the specified tenant.
    """
    await db_pool.initialize()
    async with db_pool.get_session() as session:
        query = text("""
            SELECT id, tenant_id, name, timezone
            FROM properties
            WHERE tenant_id = :tenant_id
            ORDER BY id ASC
        """)
        result = await session.execute(query, {"tenant_id": tenant_id})
        rows = result.fetchall()
        return [
            {
                "id": row.id,
                "tenant_id": row.tenant_id,
                "name": row.name,
                "timezone": row.timezone
            }
            for row in rows
        ]


async def calculate_monthly_revenue(
    property_id: str,
    tenant_id: str,
    month: int,
    year: int
) -> Dict[str, Any]:
    """
    Calculates revenue for a specific month using the property's local timezone.
    """
    return await calculate_total_revenue(
        property_id=property_id,
        tenant_id=tenant_id,
        month=month,
        year=year
    )


async def calculate_total_revenue(
    property_id: str,
    tenant_id: str,
    month: Optional[int] = None,
    year: Optional[int] = None
) -> Dict[str, Any]:
    """
    Aggregates revenue from the database with strict tenant isolation,
    timezone-aware month boundaries, and exact Decimal financial arithmetic.
    """
    # Verify property ownership first
    prop = await get_property(property_id, tenant_id)
    if not prop:
        raise PropertyNotFoundError(f"Property '{property_id}' not found for tenant '{tenant_id}'")

    timezone = prop["timezone"] or "UTC"

    await db_pool.initialize()
    async with db_pool.get_session() as session:
        if month is not None and year is not None:
            # Timezone-aware date filter matching property's local month and year
            query = text("""
                SELECT 
                    COUNT(id) AS reservation_count,
                    COALESCE(SUM(total_amount), 0) AS total_revenue,
                    COALESCE(MAX(currency), 'USD') AS currency
                FROM reservations
                WHERE property_id = :property_id 
                  AND tenant_id = :tenant_id
                  AND EXTRACT(MONTH FROM check_in_date AT TIME ZONE :timezone) = :month
                  AND EXTRACT(YEAR FROM check_in_date AT TIME ZONE :timezone) = :year
            """)
            params = {
                "property_id": property_id,
                "tenant_id": tenant_id,
                "timezone": timezone,
                "month": month,
                "year": year
            }
        else:
            # Aggregate all reservations for this property
            query = text("""
                SELECT 
                    COUNT(id) AS reservation_count,
                    COALESCE(SUM(total_amount), 0) AS total_revenue,
                    COALESCE(MAX(currency), 'USD') AS currency
                FROM reservations
                WHERE property_id = :property_id 
                  AND tenant_id = :tenant_id
            """)
            params = {
                "property_id": property_id,
                "tenant_id": tenant_id
            }

        result = await session.execute(query, params)
        row = result.fetchone()

        raw_total = Decimal(str(row.total_revenue)) if row and row.total_revenue is not None else Decimal('0')
        # Quantize exact sum to 2 decimal places using standard financial round half up
        quantized_total = raw_total.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        count = row.reservation_count if row else 0
        currency = row.currency if (row and row.currency) else "USD"

        return {
            "property_id": property_id,
            "tenant_id": tenant_id,
            "total": str(quantized_total),
            "currency": currency,
            "count": count
        }
