"""Create a real end-to-end test lead per TESTING.md: a lead + contact with
the tester's own number, plus the Webinar Opt-In custom activity so it
matches the Webinar Workflow smart view's criteria. Then verify it actually
shows up in that view.
"""
import asyncio

from outbound.config import load_settings
from outbound.close_api import CloseClient

PHONE = "+19565098952"
WEBINAR_OPT_IN_TYPE_ID = "actitype_1iw6wmALcvEY1euBXeBUG2"
WEBINAR_OPT_IN_FIELD_ID = "cf_YFKGbgBKBYvkkOylqkvNQYiiEFStXwhERDhLutMrILQ"


async def main():
    settings = load_settings()
    close = CloseClient(settings.close_api_key)
    try:
        lead = await close._request("POST", "/lead/", json_body={
            "name": "ZZ Test Jarluen",
            "contacts": [{"name": "Jarluen Test", "phones": [{"phone": PHONE, "type": "mobile"}]}],
        })
        lead_id = lead["id"]
        print(f"Created lead {lead_id}")

        activity = await close._request("POST", "/activity/custom/", json_body={
            "custom_activity_type_id": WEBINAR_OPT_IN_TYPE_ID,
            "lead_id": lead_id,
            f"custom.{WEBINAR_OPT_IN_FIELD_ID}": "Webinar Opt-In",
        })
        print(f"Created Webinar Opt-In activity {activity['id']}")

        await asyncio.sleep(2)  # let Close's search index catch up
        ids = await close.view_lead_ids(settings.smart_view_id)
        print(f"\nIn Webinar Workflow view now? {'YES' if lead_id in ids else 'NO'} ({len(ids)} leads in view)")
        print(f"\nLead ID: {lead_id}")
    finally:
        await close.aclose()


asyncio.run(main())
