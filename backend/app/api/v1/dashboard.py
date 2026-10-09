from fastapi import APIRouter, Depends, HTTPException, status, Query
from typing import Dict, Any, List, Optional
import logging
from app.services.cache import get_revenue_summary
from app.services.reservations import (
    PropertyNotFoundError,
    get_properties_for_tenant,
    get_property
)
from app.core.auth import authenticate_request as get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()


def extract_tenant_id(current_user: Any) -> str:
    """Safely extract tenant_id from AuthenticatedUser or dict."""
    tenant_id = getattr(current_user, "tenant_id", None)
    if not tenant_id and isinstance(current_user, dict):
        tenant_id = current_user.get("tenant_id")
    if not tenant_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing tenant authorization context"
        )
    return tenant_id


@router.get("/properties")
@router.get("/dashboard/properties")
async def get_tenant_properties(
    current_user: Any = Depends(get_current_user)
) -> List[Dict[str, Any]]:
    """
    Retrieve all properties owned by the authenticated tenant.
    Prevents cross-tenant discovery of properties.
    """
    tenant_id = extract_tenant_id(current_user)
    try:
        return await get_properties_for_tenant(tenant_id)
    except Exception as e:
        logger.error(f"Failed to fetch properties for tenant {tenant_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve properties"
        )


@router.get("/dashboard/summary")
async def get_dashboard_summary(
    property_id: str = Query(..., description="The property ID to retrieve summary for"),
    month: Optional[int] = Query(None, ge=1, le=12, description="Optional month (1-12) for monthly revenue"),
    year: Optional[int] = Query(None, ge=2000, le=2100, description="Optional year (e.g. 2024)"),
    current_user: Any = Depends(get_current_user)
) -> Dict[str, Any]:
    """
    Get revenue summary for a property with strict tenant verification,
    timezone-aware month boundaries, and exact decimal arithmetic.
    """
    tenant_id = extract_tenant_id(current_user)

    try:
        revenue_data = await get_revenue_summary(
            property_id=property_id,
            tenant_id=tenant_id,
            month=month,
            year=year
        )

        total_revenue_float = float(revenue_data['total'])

        return {
            "property_id": revenue_data['property_id'],
            "total_revenue": total_revenue_float,
            "currency": revenue_data['currency'],
            "reservations_count": revenue_data['count'],
            "month": month,
            "year": year
        }

    except PropertyNotFoundError:
        # Return 404 to avoid leaking existence of properties owned by other tenants
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Property '{property_id}' not found"
        )
    except Exception as e:
        logger.error(f"Error computing dashboard summary for {property_id} ({tenant_id}): {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to calculate revenue summary"
        )
