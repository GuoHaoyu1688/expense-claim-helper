# Agent operations

This is a local, noninteractive Python tool. Use the CLI on a headless Linux host. Never send receipts to an external service. All shipped examples are fictional. The default policy is an example, not a valid employer policy.

## Install and locate data

```sh
git clone https://github.com/GuoHaoyu1688/expense-claim-helper.git
cd expense-claim-helper
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
export ECH_DATA_DIR="$PWD/data/private"
.venv/bin/ech --version
```

Python 3.11+ and a POSIX filesystem are required. No API keys. SQLite lives in `$ECH_DATA_DIR/expenses.db`; copied receipts, frozen claims, policy, and backups stay beside it. `--data-dir` has highest precedence, then `ECH_DATA_DIR`, then `./data`. `--config` selects YAML; otherwise `data/config.yaml` is used. The timezone defaults to Asia/Shanghai. Restore portable archives with a fresh default config; external configured policy paths are not rewritten.

## Commands

Global flags can appear before or after subcommands: `--data-dir PATH`, `--config PATH`, `--json`, `--quiet`, `--progress`, `--yes`, `--version`. Quiet suppresses progress logs, not result JSON. Progress logs go to stderr. All commands return the same JSON envelope; `--json` requests compact output. Stdio MCP reserves stdout for protocol messages.

| Command | Arguments / behavior |
| --- | --- |
| `trip add` | Required `--start ISO --end ISO --origin CODE --destination CODE`; optional `--city-tier B --purpose SAMPLE --participants 1 --transport train` |
| `trip list` | All timed itineraries |
| `expense add` | Required `--trip ID --category NAME --amount DECIMAL`; optional `--date YYYY-MM-DD --receipt PATH --currency CNY --paid-by employee\|company --payment-method card --has-receipt --invoice-type receipt --invoice-no TEXT --nights 1 --travel-class second --note TEXT` |
| `expense list` | Optional `--trip ID` |
| `expense review ID` | `--trip ID`, `--amount DECIMAL`, `--note TEXT`, `--include` or `--exclude`; claimed records are locked |
| `receipt add PATH` | Copy orphan receipt; optional `--amount DECIMAL` for mismatch checking |
| `import-receipts PATH` | OCR CSV/JSON; `--auto-match` or explicit `--trip ID` |
| `check` | Missing receipts, orphan files, known amount mismatches, duplicate/unmatched review |
| `claim preview` | Repeated `--trip ID`, optional `--month YYYY-MM`; evaluates all included items, including already claimed ones |
| `claim build` | Same filters; `--format xlsx,pdf,docx,zip`; builds only unclaimed items/days |
| `claim list` / `claim show CODE` | Frozen documents and current status |
| `claim status CODE STATE` | Sequential `submitted`, `approved`, `paid`; `--date YYYY-MM-DD`; paid requires cumulative `--amount DECIMAL` |
| `due` | Optional `--as-of YYYY-MM-DD`; query-only reminders and balances |
| `stats` / `travel-days` | Required `--year INTEGER` |
| `export` | `--format json\|csv --out PATH`; `--kind archive\|expenses\|ledger`, `--no-bom`; overwrite needs `--yes` |
| `import PATH` | Native JSON/CSV archive into empty library; relocates files |
| `backup` | Optional `--out PATH`; otherwise timestamped ZIP |
| `serve` | Loopback only; `--host 127.0.0.1 --port 8898` |
| `mcp` | Official SDK stdio server |

Categories: flight, train, taxi, transport, lodging, meals, meeting, office, other. Common English capitalization and Chinese category input aliases are accepted; outputs use English. Amounts must be finite nonnegative decimal strings with at most two places. Offset-aware trip timestamps convert to the library timezone. Supply explicit times for accurate half-day calculations.

Real example outputs from the fictional demo:

```json
{"ok":true,"data":{"issues":[{"expense_id":11,"issues":["duplicate source","excluded pending review"]},{"expense_id":12,"issues":["receipt amount mismatch"]}],"orphan_receipts":[],"issue_count":2},"warnings":[]}
```

```json
{"ok":false,"error":{"code":"INVALID_AMOUNT","message":"Invalid amount with at most two decimals: NaN"}}
```

Exit codes: `0` success, `1` processing/I/O failure, `2` invalid arguments/configuration/data, `3` missing resource. Never infer success from an existing output filename: read `ok` and the process status. JSON details can contain absolute paths; redact them before sharing.

## Idempotence and safety

Identical trip and expense inputs reuse records. OCR row identities and receipt content hashes prevent repeated ingestion; imported duplicates stay excluded pending review. A native archive can be reapplied only when its digest matches the previous successful restore; otherwise the library must be empty. Claims reserve expense IDs and allowance dates, freezing a YAML hash and receipt bytes. Repeated exhausted scopes return the old claim; requesting a new format adds that format. New item scopes create another claim. Do not edit SQLite to bypass locks.

Paid `--amount` is cumulative; unchanged values do not create extra cash flows. The workflow has no backward status changes. Excluded records do not increase advance balances. Different same-date trips share one calendar-day allowance cap. Preview CLI includes all items for analysis; the Web preview and build use remaining items only. Review policy/check warnings before building.

Before replacing exports, restoring archives, or manually changing policy/storage, run `ech backup` and retain the result. `--yes` only authorizes replacing the explicit export destination. Never infer authority to submit, email, or upload a claim from a request to generate it. This tool has no external submission function.

## MCP

```sh
ECH_DATA_DIR=/absolute/private/data /absolute/repo/.venv/bin/ech mcp
```

Client configuration:

```json
{"mcpServers":{"expenses":{"command":"/absolute/repo/.venv/bin/ech","args":["--data-dir","/absolute/private/data","mcp"]}}}
```

Tools: `add_trip(start_at,end_at,origin,destination,city_tier,purpose_code,participants,transport)`, `add_expense(trip_id,category_name,amount,expense_date,receipt,paid_by,nights,travel_class,note)`, `check_missing()`, `build_claim(trip_ids,formats,month)`, `list_pending_claims()`, `travel_days(year)`. The first two write records; build writes immutable artifacts and reserves items. Others only read. Defaults match CLI. Each returns the standard envelope. Exact SDK input schemas are in `docs/mcp-tools.json`.

## Recipes

1. “Prepare these travel receipts.” Add the timed trip, run `import-receipts PATH --auto-match --json`, run `check --json`, review unmatched/duplicate rows, then `claim build --trip ID --format xlsx,pdf,docx,zip --json`.
2. “How much am I still waiting for?” Run `due --json`. Explain `advance_balance_cents` separately from `outstanding_claim_cents` and identify partial payments.
3. “Record another reimbursement.” Read `claim show CODE --json`; add the new payment to existing `paid_cents`, then set paid with the new cumulative amount and date. Do not overwrite the cumulative total with the increment.
4. “Give me the January claim.” Run `claim preview --month 2026-01 --json`, then `claim build --month 2026-01 --format xlsx,pdf,zip --json`. Explain that previously claimed rows/days are omitted from the new build.
5. “Move my records.” Run `backup`, export a native JSON archive, then import it with `--data-dir` pointing to an empty directory. Verify `trip list`, `claim list`, and receipt paths. Keep the original backup until verified.

## Development checks

```sh
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest --cov=expense_claim_helper --cov-report=term-missing
```

Tests include actual SDK stdio, local HTTP, generated document parsing, exact monetary expectations, archive escape rejection, and a self-contained OCR integration fixture. Keep fixtures fictional. Do not commit `data/`, databases, `.env`, `.venv`, `.coverage*`, screenshots containing private data, or output archives. Never use float arithmetic for money. Keep CLI, Web and MCP using the shared core.
