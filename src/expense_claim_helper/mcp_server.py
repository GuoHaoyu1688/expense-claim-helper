"""Official FastMCP stdio integration sharing the CLI core."""

from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .core.common import failure, settings, success
from .core.store import Store


def create_server(root: Path, config: dict) -> FastMCP:
    server = FastMCP("expense-claim-helper")

    def call(method, *args, **kwargs):
        store = None
        try:
            store = Store(root, config)
            return success(getattr(store, method)(*args, **kwargs))
        except Exception as exc:
            return failure(exc)[0]
        finally:
            if store:
                store.close()

    @server.tool()
    def add_trip(
        start_at: str,
        end_at: str,
        origin: str,
        destination: str,
        city_tier: str = "B",
        purpose_code: str = "SAMPLE",
        participants: int = 1,
        transport: str = "train",
    ) -> dict:
        """Write an itinerary with ISO times; identical input reuses the trip."""
        return call(
            "add_trip",
            start_at,
            end_at,
            origin,
            destination,
            city_tier,
            purpose_code,
            participants,
            transport,
        )

    @server.tool()
    def add_expense(
        trip_id: int,
        category_name: str,
        amount: str,
        expense_date: str | None = None,
        receipt: str | None = None,
        paid_by: str = "employee",
        nights: int = 1,
        travel_class: str = "",
        note: str = "",
    ) -> dict:
        """Write an expense and copy an optional local receipt. Money is a decimal string."""
        return call(
            "add_expense",
            trip_id,
            category_name,
            amount,
            expense_date,
            receipt=receipt,
            paid_by=paid_by,
            nights=nights,
            travel_class=travel_class,
            note=note,
        )

    @server.tool()
    def check_missing() -> dict:
        """Read missing/orphan files, amount mismatches and duplicate/unmatched warnings."""
        return call("check")

    @server.tool()
    def build_claim(
        trip_ids: list[int], formats: list[str] | None = None, month: str | None = None
    ) -> dict:
        """Write immutable claim snapshots and local reports; reserve claimed items."""
        return call("build_claim", trip_ids, formats, month)

    @server.tool()
    def list_pending_claims() -> dict:
        """Read reminders, unpaid amounts and employee advance balance."""
        return call("due")

    @server.tool()
    def travel_days(year: int) -> dict:
        """Read annual local-calendar travel days, capped at one day per date."""
        return call("travel_days", year)

    return server


def main():
    root, config = settings()
    create_server(root, config).run(transport="stdio")


if __name__ == "__main__":
    main()
