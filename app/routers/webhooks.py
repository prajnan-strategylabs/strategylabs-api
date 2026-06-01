import logging
from typing import Dict, Any
from fastapi import APIRouter, Header, HTTPException, status
from app.db import get_db
from app.config import get_settings

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
log = logging.getLogger("app.routers.webhooks")

@router.post("/revenuecat")
async def revenuecat_webhook(
    payload: Dict[str, Any],
    authorization: str | None = Header(None)
):
    settings = get_settings()
    # Validate authorization token if configured in settings
    if settings.revenuecat_webhook_auth and authorization != settings.revenuecat_webhook_auth:
        log.warning(f"Unauthorized RevenueCat webhook attempt. Header: {authorization}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization token"
        )

    event = payload.get("event")
    if not event:
        log.error(f"RevenueCat webhook payload missing 'event': {payload}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing event data"
        )

    event_type = event.get("type")
    user_id = event.get("app_user_id")
    
    # Handle RevenueCat TEST event ping
    if event_type == "TEST":
        log.info("Received RevenueCat TEST webhook event successfully.")
        return {"ok": True, "message": "TEST event received"}

    entitlements = event.get("entitlement_ids") or []

    log.info(f"Received RevenueCat webhook: type={event_type}, user_id={user_id}, entitlements={entitlements}")

    if not user_id:
        return {"ok": True, "message": "No app_user_id, ignored"}

    # We care about StrategyLabs Auto and StrategyLabs Trader entitlements
    our_entitlements = {"StrategyLabs Auto", "StrategyLabs Trader"}
    has_our_entitlement = any(e in our_entitlements for e in entitlements) or event.get("entitlement_id") in our_entitlements
    
    if not has_our_entitlement:
        log.info(f"Webhook event for user {user_id} does not affect our entitlements. Ignored.")
        return {"ok": True, "message": "Not our entitlement"}

    db = get_db()
    
    # Entitlement activation / renewal events
    upgrade_events = {
        "INITIAL_PURCHASE",
        "RENEWAL",
        "NON_RENEWING_PURCHASE",
        "UNCANCELLATION"
    }
    
    # Entitlement revocation / cancellation events
    downgrade_events = {
        "CANCELLATION",
        "EXPIRATION",
        "BILLING_ISSUE"
    }

    try:
        if event_type in upgrade_events:
            # Determine which tier to upgrade to
            active_entitlements = set(entitlements)
            if event.get("entitlement_id"):
                active_entitlements.add(event.get("entitlement_id"))
                
            if "StrategyLabs Auto" in active_entitlements:
                target_tier = "auto"
            elif "StrategyLabs Trader" in active_entitlements:
                target_tier = "trader"
            else:
                target_tier = "free"
                
            db.table("profiles").update({"tier": target_tier}).eq("id", user_id).execute()
            log.info(f"Upgraded user {user_id} to {target_tier} via webhook")
        elif event_type in downgrade_events:
            # Check what entitlements remain active
            active_entitlements = set(entitlements)
            revoked = event.get("entitlement_id")
            if revoked in active_entitlements:
                active_entitlements.remove(revoked)
                
            if "StrategyLabs Auto" in active_entitlements:
                target_tier = "auto"
            elif "StrategyLabs Trader" in active_entitlements:
                target_tier = "trader"
            else:
                target_tier = "free"
                
            db.table("profiles").update({"tier": target_tier}).eq("id", user_id).execute()
            log.info(f"Downgraded/Updated user {user_id} to {target_tier} via webhook")
        else:
            log.info(f"Event type {event_type} ignored")
            
        return {"ok": True}
    except Exception as e:
        log.error(f"Error handling RevenueCat webhook for user {user_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update user profile"
        )
