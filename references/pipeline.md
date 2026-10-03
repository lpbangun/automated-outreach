# Execution and provider accounting

Python 3.10+ on Linux/macOS or WSL; standard library only. The host interprets the brief and invokes providers/models. There is no background service, automatic sender or JobSSS dependency. CLI commands are listed by `python3 scripts/outreach.py --help`.

## Inputs and output

Normalize a brief into JSON with `company`, `domain`, `purpose`, `search_titles` (1–5), `sender_summary` and optional `source_snapshot_sha256`. `plan SPEC --out-dir RUN` prepares bounded requests; it does not dispatch them. Recheck the live catalog before dispatch: planned endpoint names, schemas and nominal ceilings can change.

`normalize RECORDING DOMAIN` extracts discovery candidates; `rank CANDIDATES RESPONSES` checks typed Jev results. Provider rows and ranks are leads, not identity verification.

`compile SPEC EVIDENCE --out-dir RUN` emits JSON and Markdown. EVIDENCE supplies `contacts` (at most two per brief) with `name`, `role`, `relevance`, `channel`, `channel_reason`, `identity_sources`, `evidence` and `draft`, plus a host `review`. For larger research requests use separate briefs or an explicitly adapted schema; do not silently drop requested contacts. Every source has an HTTPS `url`, timezone-aware `retrieved_at` and `published_at` (ISO date/time or null). Two distinct current-role sources are required by the strict compiler. Keep historical sources outside that gate; report held leads separately instead of falsely marking sources current. Structural checks do not attest source truth.

## Private state and credentials

Keep private runs outside the installed skill/repository. Pass an explicit ledger path to share one budget across related runs. The CLI default is `.outreach/budget.json` relative to the working directory; it never reads an evaluation's sibling configuration.

Use `TREG_TOKEN` and `OPENROUTER_API_KEY`, or `TREG_KEY_FILE` / `OPENROUTER_KEY_FILE` pointing to user-owned regular files with permissions 0600 outside the project. Optional `OUTREACH_CONFIG` explicitly points to JSON with `treg_key_file` and/or `openrouter_key_file`. A connected Treg MCP may provide authentication without exposing a token. Do not claim direct HTTP authentication when only MCP executed successfully.

## Spending

Do not infer authorization from the example below. Initialize only the user's authorized provider caps; there is no default paid budget or cross-provider transfer.

```sh
python3 scripts/outreach.py --ledger /private/run/budget.json init \
  --treg-usd 0.50 --openrouter-usd 0.50 \
  --authorization 'User-authorized provider caps for this run'
```

Check catalog parameters, prices and available balance before Treg execution. `reserve PROVIDER REQUEST_ID MAX_USD` precedes every connected MCP call, including retries. Save the full response before `settle REQUEST_ID ACTUAL_USD`. Request IDs are unique local dispatch IDs; preserve provider IDs separately. Unknown charges retain reservations and block re-dispatch of that request. Reconcile against provider audit before retrying; missing cost is not zero. The ledger uses process locks and upward-rounded micro-USD, while recordings retain exact decimal cost. Native agent costs are separate and may be unavailable.

Direct `call PROVIDER REQUEST_JSON --out FILE` reserves, dispatches, records and settles. Treg requests contain `endpoint_id`, `params`, `max_usd`, and optional catalog-documented `method` (GET or POST). Direct dispatch uses a server-side maximum-cost header and idempotency key. With MCP, the host must apply catalog-documented ceilings/idempotency settings where supported and perform the external reservation itself.

Jev uses the OpenRouter Decisions endpoint and documented model `typesafe/jev-1.13`. The adapter fetches live endpoint pricing/credit, requires the expected single 32,000-token endpoint, conservatively reserves its full context cost, and rejects unexpected fees or shapes. If provider assumptions change, stop and update the adapter rather than bypassing checks. Record response `usage.cost`; missing cost retains the hold. Jev is a typed decision model, not the drafting model.

## Email

`email-plan SPEC SELECTED` prepares `treg.people.email.find` for exact identities, company domain and profile. `verify-plan FINDING` verifies the returned address only. Prices and schemas still require live catalog checks. A second SMTP-aware verification may resolve an identity-only unknown result within the authorized budget; avoid repeatedly purchasing the same lookup.

`email-assess PERSON FINDING VERIFICATION...` preserves identity mismatch, verifier disagreement, SMTP/catch-all status and cached verification dates. It accepts routed `body.output`, Hunter `body.data` and native verifier bodies. Identity-positive `valid-risky` with unknown SMTP followed by positive non-catch-all SMTP is complementary evidence; negative identity or SMTP results conflict. Delivery is separate from mailbox ownership and current hiring authority. Provider UTC dates can differ from the user's local date; preserve both without inventing a new test date.

## X and professional activity

`x-plan PERSON` requires an `x_profile_url` and two distinct HTTPS `x_identity_sources` with `supports_same_person: true`, then prepares `treg.x.user.posts`. These booleans require actual host source review. `x-normalize PERSON RECORDING --retrieved-at ISO_TIMESTAMP` checks author handles, dates and canonical URLs. The host checks relevance and source coverage before selecting a hook. Relative publication dates stay unknown unless resolved by a source; undated posts are not recent. No confirmed account is an acceptable research result. Activity does not establish open DMs.

## Validation and limits

Run `python3 -m unittest discover -s scripts -p 'test_*.py'`. Tests use synthetic fixtures and check budget, identity/date, channel and compilation behavior; they do not prove live provider availability or outreach effectiveness. Compare raw alternatives using [role briefs](roles.md) and an independent review when available. Final drafts remain unsent.

Provider references: [OpenRouter Decisions](https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-request), [Jev](https://openrouter.ai/typesafe/jev-1.13), [Treg API](https://treg.to/llms.txt). Recheck them before live execution.
