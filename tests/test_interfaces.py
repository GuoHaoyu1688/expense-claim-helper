import asyncio
import json
import os
import sys
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from expense_claim_helper.cli import main
from expense_claim_helper.core.common import ClaimError, settings
from expense_claim_helper.mcp_server import create_server
from expense_claim_helper.web.server import make_server


def test_cli_workflow(tmp_path, capsys, receipt):
    common = ["--data-dir", str(tmp_path / "cli"), "--json"]

    def run(args):
        assert main([*args, *common]) == 0
        return json.loads(capsys.readouterr().out)["data"]

    trip = run(
        [
            "trip",
            "add",
            "--start",
            "2026-01-01T08:00",
            "--end",
            "2026-01-02T18:00",
            "--origin",
            "CITY-A",
            "--destination",
            "CITY-B",
        ]
    )
    run(
        [
            "expense",
            "add",
            "--trip",
            str(trip["id"]),
            "--category",
            "train",
            "--amount",
            "100.00",
            "--receipt",
            str(receipt),
            "--travel-class",
            "second",
        ]
    )
    run(["trip", "list"])
    run(["expense", "list"])
    run(["check"])
    run(["travel-days", "--year", "2026"])
    run(["stats", "--year", "2026"])
    run(["due"])
    claim = run(["claim", "build", "--trip", "1", "--format", "xlsx,pdf,zip"])
    run(["claim", "preview", "--trip", "1"])
    run(["claim", "list"])
    run(["claim", "show", claim["code"]])
    run(["claim", "status", claim["code"], "submitted"])
    out = tmp_path / "native.json"
    run(["export", "--format", "json", "--out", str(out)])
    run(["backup"])
    assert main(["claim", "show", "missing", *common]) == 3
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "NOT_FOUND"
    assert main(["trip", "list", "--invalid", *common]) == 2
    json.loads(capsys.readouterr().out)


def test_all_mcp_tools(tmp_path, receipt):
    root, config = settings(tmp_path / "mcp")
    server = create_server(root, config)

    async def run():
        assert len(await server.list_tools()) == 6

        async def call(name, args):
            return json.loads((await server.call_tool(name, args))[0].text)

        trip = await call(
            "add_trip",
            {
                "start_at": "2026-01-01T08:00",
                "end_at": "2026-01-02T18:00",
                "origin": "CITY-A",
                "destination": "CITY-B",
            },
        )
        expense = await call(
            "add_expense",
            {
                "trip_id": trip["data"]["id"],
                "category_name": "train",
                "amount": "100.00",
                "receipt": str(receipt),
                "travel_class": "second",
            },
        )
        assert expense["ok"]
        assert (await call("check_missing", {}))["ok"]
        assert (await call("travel_days", {"year": 2026}))["data"]["travel_days"] == "2"
        claim = await call("build_claim", {"trip_ids": [trip["data"]["id"]], "formats": ["pdf"]})
        assert claim["ok"]
        assert (await call("list_pending_claims", {}))["ok"]
        assert not (
            await call("add_expense", {"trip_id": 999, "category_name": "other", "amount": "1.00"})
        )["ok"]

    asyncio.run(run())


def test_actual_stdio(tmp_path):
    async def run():
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "expense_claim_helper.cli", "--data-dir", str(tmp_path / "stdio"), "mcp"],
            env=dict(os.environ),
        )
        async with stdio_client(parameters) as (read, write), ClientSession(read, write) as client:
            await client.initialize()
            assert len((await client.list_tools()).tools) == 6
            result = await client.call_tool("travel_days", {"year": 2026})
            assert json.loads(result.content[0].text)["data"]["travel_days"] == "0"

    asyncio.run(run())


def test_http(store, trip):
    server = make_server(store.root, store.config, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def post(path, body, origin=None):
        return json.load(
            urlopen(
                Request(
                    base + path,
                    data=json.dumps(body).encode(),
                    headers={"Content-Type": "application/json", "Origin": origin or base},
                )
            )
        )

    try:
        assert b"journey ledger" in urlopen(base + "/").read()
        assert len(json.load(urlopen(base + "/api/dashboard?year=2026"))["data"]["trips"]) == 1
        assert json.load(urlopen(base + "/api/trip?id=1"))["ok"]
        assert post(
            "/api/expense", {"trip_id": trip["id"], "category_name": "meals", "amount": "25.00"}
        )["ok"]
        claim = post("/api/build", {"trip_ids": [1], "formats": ["pdf"]})["data"]
        assert (
            urlopen(base + "/api/download?code=" + claim["code"] + "&format=pdf")
            .read()
            .startswith(b"%PDF")
        )
        with pytest.raises(HTTPError):
            post("/api/build", {"trip_ids": [1]}, "http://evil.invalid")
        with pytest.raises(HTTPError):
            urlopen(base + "/api/download?code=../../etc/passwd&format=pdf")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    with pytest.raises(ClaimError):
        make_server(store.root, store.config, "0.0.0.0", 0)


def test_real_receipt_project_fixture(store):
    source = Path(__file__).parents[1] / "examples/receipts.csv"
    store.add_trip("2026-01-01T08:00", "2026-01-10T18:00", "CITY-A", "CITY-B")
    result = store.import_receipts(str(source), True)
    assert (
        result["imported"] == 11 and result["unmatched"] == 0 and result["excluded_duplicates"] == 1
    )
    rows = store.expenses()
    assert len([row for row in rows if row["included"]]) == 10
    assert all(Path(row["receipt_path"]).is_file() for row in rows)
    assert sum(row["amount_cents"] for row in rows if row["included"]) == 118000
