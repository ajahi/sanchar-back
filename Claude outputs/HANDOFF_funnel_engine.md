# Task: Replace Sanchar's conversation logic with a Markdown-defined funnel engine

You are working inside `nepsocial-backend`, the FastAPI backend of **Sanchar AI**. Sanchar is a multi-tenant
customer-support and sales automation product for businesses that sell over Instagram DMs, Facebook Messenger
and WhatsApp. It talks to customers in English, Nepali (Devanagari) and Romanized Nepali ("Nepglish").

Read this whole document before changing anything. **Start with Phase 0 (discovery) and report back before writing code.**

Companion files that come with this document. Put them in the repo at the paths shown:

| File | Put it at | What it is |
|---|---|---|
| `restaurant_order.md` | `funnels/templates/restaurant_order.md` | First funnel template (restaurant/café order taking) |
| `funnel_tests_restaurant.yaml` | `tests/funnels/restaurant.yaml` | Test suite R1–R7 for that funnel |
| `README.md` | `funnels/README.md` | Human docs for funnels and test cases |
| `cafe_2026-11-12_case1.json`, `cafe_2026-11-12_case2.json` | `fixtures/transcripts/restaurant_failed/` | Real failed conversations, anonymised and annotated |

---

## 1. Why we are doing this

- One real restaurant client is live. The bot worked about **60% of the time** but became **annoying near the end of
  conversations**: it re-asked things, confirmed repeatedly, wrote long messages and lost track of the order. So
  customers **phoned the shop instead**. That outcome is unacceptable.
- Conversation behaviour is currently hardcoded per business type. We want each business *category*
  (restaurant, real estate, content agency, …) defined as a **Markdown template** that a super-admin publishes and
  a tenant selects.
- We also need **post-conversation actions**: POST JSON to a tenant website, send notifications, update another
  app, trigger webhooks. These must be configurable per tenant.

## 2. Hard constraints

1. **Do not change the frontend** (`next-app`, a separate repo). Every existing API endpoint must keep its path,
   request shape and response shape. New endpoints are allowed only if they are additive.
2. **Do not touch** channel ingestion (the Meta/WhatsApp webhooks), auth, or tenant/user management, beyond
   reading from them.
3. **The new engine sits behind a per-tenant flag** (`conversation_engine: legacy | funnel`, default `legacy`).
   A conversation stays on the engine it started with until it ends. Never switch engines mid-conversation.
4. **The LLM never does these things:** arithmetic, deciding availability or prices, calling side-effect tools
   outside the allowed state, or firing webhooks. Code does all of them.
5. Reuse the existing queue system and Postgres (async SQLAlchemy). Use Alembic migrations if the repo already has them.
6. Keep LLM calls to a minimum. We are on Groq with rate limits. Load only the active funnel's instructions and
   examples into the prompt.

## 3. Target architecture

```
incoming message (existing webhook → existing queue)
        │
        ▼
 engine switch (tenant.conversation_engine) ──► legacy path (unchanged)
        │ funnel
        ▼
 Router: picks the active funnel from the tenant's enabled funnels (by each template's `trigger` text)
        │
        ▼
 Funnel engine (per turn)
   1. load state: stage, slots, cart  ← Postgres, keyed by (tenant_id, conversation_id)
   2. extract: one LLM call returns structured JSON of slot/cart updates from the new message
      (fill EVERY slot it can; never overwrite a filled slot unless the user changes it)
   3. validate + apply updates in code; run state transitions defined in the template frontmatter
   4. on entering a state, run its `on_enter` tools in CODE (e.g. confirm → lookup_items, lookup_delivery, quote_cart)
   5. reply: one LLM call with the template's Markdown body + an injected STATE SUMMARY
      (cart, filled slots, missing slots, tool results) + only the tools allowed in this stage
   6. persist state; emit outbox events for state changes
        │
        ▼
 Outbox worker (existing queue) ──► tenant actions: http_post | webhook | notify | app_update
```

The two LLM calls per turn (extract and reply) are the target. If discovery shows the current code already
does extraction differently, propose an alternative in the Phase 0 report.

## 4. Funnel template format

A template is a Markdown file with YAML frontmatter. See `restaurant_order.md` for the full example.

- **Frontmatter (enforced by code):** `funnel`, `version`, `trigger`, `slots` (type, required, required_if),
  `extraction` rules, `memory` rules, `states` (each with `tools` and/or `on_enter`, `requires_user_yes`),
  `guards`.
- **Markdown body (given to the LLM):** tone, the "do not be annoying" rules, and example conversations.
  The current examples are synthetic placeholders. **They will be replaced with anonymised real transcripts.**
  Build the engine so the examples section is only loaded while that funnel is active.

Write a parser that validates frontmatter against a Pydantic schema and fails loudly on unknown tools,
unknown slots or unreachable states.

### Anti-annoyance contract (enforce in code, not only in the prompt)
- Never ask for a slot that is already filled. Code computes `missing_slots`; the reply prompt may only ask for those.
- At most one question per bot message. Missing slots are asked for together in one sentence.
- Exactly one confirmation prompt. Any cart change after confirm re-runs `confirm.on_enter` and re-confirms once.
- Bot messages are at most about 3 short lines. Repeat the full cart only at confirm, or when the user asks for it.
- On a digression (FAQ mid-order): answer it, then add a one-line nudge back to the order. Never drop the cart.
- Cart TTL (6h): after the TTL, ask "continue the earlier order or start a new one?" Never drop it silently.

## 5. Restaurant category specifics

Read the existing restaurant flow during discovery. It already POSTs orders to a real restaurant website.
Keep that integration and wrap it as the `post_order` tool.

- **Items:** listed from the tenant's website during the conversation (`search_menu`, fuzzy matching with
  aliases, Devanagari and Romanized spellings; e.g. rapidfuzz or pg_trgm plus a transliteration table).
- **At confirm (`on_enter`, run by code):**
  - `lookup_items`: a LIVE lookup on the tenant website for availability and current price. **The live result always
    wins** over what was listed earlier. Unavailable items are removed, the rest are kept, and the customer is told.
    A changed price is re-quoted. If the lookup fails, do not submit; notify the owner.
  - `lookup_delivery`: check the website's deliverable list first, then the tenant's **staff-confirmed locations**
    (a list the tenant maintains in the dashboard, each with its own fee). Match fuzzily and handle aliases. If the
    location is in neither list, offer pickup and keep the cart.
  - `quote_cart`: subtotal, delivery fee, discounts, total. **Every Rs amount in any bot message must come from
    this result.**
- **Store status (pre-check, every turn, before any slot question):** `get_store_status` combines regular hours,
  last-order cutoff, closed days and **ad-hoc closures** (a new `closures` table: tenant_id, start, end, message,
  allow_preorder; tenant-editable). If the store is closed, go to stage `closed` and ask **no** slot questions.
  Reply with the tenant's closure message, and offer pre-order only if `allow_preorder`. Never call `post_order`.
  - *Known production bug this fixes:* the tenant wrote "closed today" in the knowledge base, but the bot kept asking
    "delivery or pickup?", because RAG only retrieves that chunk when the query matches it, and the fixed
    question order outranked it.
  - *Legacy compatibility:* until tenants move to `closures`, treat KB entries flagged as **pinned notices** (with
    `valid_until`) as closures. Always inject them; never rely on retrieval. In Phase 0, check whether the KB
    model can carry a flag like that.
- **Fulfilment inference (code, after extraction):** never ask "delivery or pickup?" if any rule in the template's
  `fulfilment.infer` fires. A filled address or delivery zone, or a delivery verb, means `delivery`. A pickup verb
  means `pickup`. A tenant branch name plus a pickup verb means `pickup` with `branch` set, not `address`.
  - *Known production bug this fixes:* customers wrote "deliver to <location>" and still got asked "delivery or pickup?".
  - Add the verb lists for English, Romanized Nepali and Devanagari to the template, not to code. Include the
    "bring" verbs (`lyaidinu`, `lyaidena`, `lyaunu`, …). Both real failures used them.
- **Landmark addresses:** "Expert, 2nd floor" is a valid address. Derive `delivery_zone` from a tenant-maintained
  landmark → zone map. If it can't be resolved, ask once and specifically ("Expert kun area ma parcha?"). Never ask
  for the address again.
- **Slot merging:** a clarifying answer ("chicken") updates the pending item and keeps its quantity and every other
  slot. Real bug: 2 → 1, and fulfilment and address were asked again.
- **Intents handled by code:** `staff_unreachable` ("phone uthena") → `notify.owner` with cart and slots, offer to take
  the order in chat. `wants_human` → `handoff.requested` with cart, slots, missing slots and the last message, and pause
  the bot until an operator replies or 15 min pass. The handoff payload must let the operator continue without
  re-asking anything. Real bug: the operator took over 23 minutes later and had to ask for the phone number.
- **Never greet again** after the first bot message, and never drop an open cart. Real bug: "Namaste! Kasto sahayog
  chahiyo?" in the middle of an order.
- **Language and template leakage:** reply in the customer's language. Never put example place names from the
  template into a reply. Real bug: "e.g., chipledhunga, bagale tole" was copied from the prompt into English replies
  to Nepglish customers. While in Phase 0, check the current prompt for that example text.

### Real failure transcripts (use them)
`fixtures/transcripts/restaurant_failed/cafe_2026-11-12_case1.json` and `…_case2.json` are anonymised real
conversations. Every bad bot turn is tagged with a failure code (F1–F12), and each file has `replay_points` with the
expected behaviour. They are the primary acceptance test. Add new failed transcripts in the same format.
- **Submit:** `post_order` is exposed only in `submit`. Tell the customer the order is placed only after it succeeds.

> Note: if staff-confirmed locations need storage and there is no dashboard UI for them yet, add the table and
> an additive admin endpoint. Do **not** build frontend UI. Report it as a frontend follow-up.

## 6. Data model (additive tables)

| Table | Key columns |
|---|---|
| `funnel_templates` | id, category, name, current_published_version, replacement_template_id (nullable), archived_at |
| `funnel_template_versions` | id, template_id, version, frontmatter JSONB, body_markdown, examples JSONB, includes JSONB (block key@version), status (`draft`/`testing`/`published`/`archived`), validation_errors JSONB, last_test_run_id, created_by, created_at, published_at |
| `behaviour_blocks` | id, key, version, body_markdown, status (`active`/`archived`), created_by, created_at |
| `funnel_test_runs` | id, template_version_id, status, hard_passed, soft_pass_rate, report JSONB, started_at, finished_at |
| `funnel_audit_log` | id, actor_id, action, template_id, version, tenant_id, diff JSONB, test_run_id, created_at |
| `tenant_funnels` | id, tenant_id, template_id, template_version (pinned), overrides JSONB (hours, zones, tone, examples, website endpoints), enabled |
| `conversation_funnel_state` | conversation_id (PK/FK), tenant_id, engine, funnel_id, template_version, stage, slots JSONB, cart JSONB, updated_at, expires_at |
| `staff_confirmed_locations` | id, tenant_id, name, aliases[], delivery_fee, active |
| `tenant_actions` | id, tenant_id, on_event, type (`http_post`/`webhook`/`notify`/`app_update`), config JSONB (url, payload template, secret ref, `when` condition), retry policy, enabled |
| `outbox_events` | id, tenant_id, conversation_id, type, payload JSONB, created_at, processed_at |
| `action_deliveries` | id, event_id, action_id, idempotency_key (unique), attempt, status (`pending`/`succeeded`/`failed`/`dead_lettered`), response_code, response_excerpt, next_retry_at |

Add `tenants.conversation_engine` (default `legacy`).

## 7. Post-conversation actions

- **Events** (written to `outbox_events` in the **same transaction** as the state change): `order.confirmed`,
  `order.submitted`, `order.submit_failed`, `lookup.failed`, `conversation.ended`, `handoff.requested`.
- **`conversation.ended`:** DMs never formally end. Use a delayed job on the existing queue (e.g. 30 min idle) that
  every new message resets. Classify the outcome (`ordered`/`abandoned`/`handoff`) in code from the state.
  An optional LLM summary must be strict JSON validated by a schema.
- **Worker:** reads unprocessed outbox rows, matches `tenant_actions` by `on_event` and `when`, and renders
  the payload with sandboxed Jinja from the conversation state. It sends with:
  - an idempotency key `event_id:action_id` on every attempt
  - an HMAC-SHA256 signature header using a per-action secret
  - exponential backoff retries, then dead-letter, plus a `notify.owner` event on dead-letter
- **Delivery log:** add an additive read endpoint (`GET /api/v1/tenants/{id}/action-deliveries`) and a retry
  endpoint. No frontend work.

## 8. Super-admin: manage funnels and Markdown behaviour (backend only)

The super-admin must be able to **add, edit and remove funnels and their Markdown behaviour without a deploy**.
Templates live in the database. The files in `funnels/templates/` are only seed data, loaded by an idempotent
seed command.

### 8.1 What the super-admin manages

| Thing | Add | Edit | Remove |
|---|---|---|---|
| **Funnel template** (category + frontmatter + Markdown body) | Upload a `.md` file or create from JSON | Creates a **new draft version**. Published versions are immutable. | **Archive**: hidden from tenants, no new conversations. Hard-delete is allowed only for a draft that was never published. |
| **Behaviour blocks**: reusable Markdown snippets (global anti-annoyance rules, Nepglish style guide, safety/escalation rules, category tone) | Create a block with a `key` | New block version | Archive. Blocked while a published template still includes it. |
| **Template ↔ block links**: a template lists `includes: [global_rules@2, nepglish_style@1]` in its frontmatter | Add an include | Change the pinned block version (makes a new template draft) | Remove an include (makes a new template draft) |
| **Examples section** (real anonymised transcripts, good/bad) | Add an example | Edit an example | Remove an example. Every change makes a new template draft. |
| **Tenant ↔ funnel assignment** | Assign a published template to any tenant | Change the pinned version or the overrides | Unassign |

The prompt the engine assembles for a turn is:
**included behaviour blocks (in order) → template body → tenant overrides (tone/examples) → state summary.**
Treat the assembled prompt as a cache artifact keyed by `(template_version, block_versions, tenant_override_hash)`.

### 8.2 Lifecycle rules

- `draft → testing → published → archived`. Only `published` versions are visible to tenants.
- **Publish gate:** saving a draft validates the frontmatter (schema, known tools, known slots, reachable states,
  resolvable `includes`). Moving to `published` runs the category's test suite (section 9) against a fixture tenant.
  Publishing is blocked unless all hard assertions pass and soft assertions meet `pass_threshold`. Store the run
  result on the version.
- **Version pinning:** tenants pin a version. Publishing a new version does **not** move tenants automatically.
  The super-admin chooses to roll forward per tenant, or for all tenants of a category.
- **Removing (archiving) a template or version that tenants use:**
  - The endpoint returns the affected tenants and needs `?confirm=true`.
  - Conversations already in progress finish on their pinned version.
  - New conversations for affected tenants use either the template's `replacement_version` (if the super-admin
    set one) or `conversation_engine=legacy`. Emit a `funnel.archived` event so the actions worker can notify
    the tenant owner.
- **Rollback:** re-pin tenants to any earlier published version with one call.
- **Audit log:** every create, edit, publish, archive, assign and rollback is recorded with actor, timestamp,
  before/after diff and the test-run id.

### 8.3 Endpoints (additive, super-admin role only)

```
POST   /api/v1/admin/funnel-templates                      # create draft (JSON or .md upload)
GET    /api/v1/admin/funnel-templates?category=&status=
GET    /api/v1/admin/funnel-templates/{id}/versions
GET    /api/v1/admin/funnel-templates/{id}/versions/{v}    # includes rendered/assembled prompt preview
PUT    /api/v1/admin/funnel-templates/{id}/draft           # edit draft (frontmatter, body, examples, includes)
POST   /api/v1/admin/funnel-templates/{id}/validate
POST   /api/v1/admin/funnel-templates/{id}/versions/{v}/test      # run category suite (async job → run id)
POST   /api/v1/admin/funnel-templates/{id}/versions/{v}/publish
POST   /api/v1/admin/funnel-templates/{id}/versions/{v}/archive?confirm=true
DELETE /api/v1/admin/funnel-templates/{id}                 # only if never published

POST   /api/v1/admin/behaviour-blocks                      # create
PUT    /api/v1/admin/behaviour-blocks/{key}                # new version
POST   /api/v1/admin/behaviour-blocks/{key}/archive
GET    /api/v1/admin/behaviour-blocks/{key}/usages         # which templates include it

POST   /api/v1/admin/funnel-templates/{id}/versions/{v}/sandbox-chat  # dry-run turns vs fixture tenant, no side effects
POST   /api/v1/admin/tenants/{tenant_id}/funnels           # assign {template_id, version, overrides}
PATCH  /api/v1/admin/tenants/{tenant_id}/funnels/{fid}     # change version/overrides/enabled
DELETE /api/v1/admin/tenants/{tenant_id}/funnels/{fid}     # unassign
POST   /api/v1/admin/funnel-templates/{id}/rollout         # {to_version, tenant_ids | "all"}
GET    /api/v1/admin/funnel-audit-log?template_id=&tenant_id=
```

`sandbox-chat` runs the real engine with mocked tools and **never** fires `post_order` or tenant actions.
The super-admin uses it to try a draft before publishing.

### 8.4 Tenant side (additive)
- List published templates for the tenant's category; enable or disable assigned funnels; edit **only** the
  overrides the template marks as tenant-editable (`tenant_editable: [hours, tone, examples, staff_confirmed_locations]`
  in the frontmatter).
- Set `conversation_engine`, or leave that to the super-admin only (decide in Phase 0 based on the existing roles).

### 8.5 Frontend
No frontend work in this task. The endpoints must be usable from Swagger/OpenAPI with clear request and response
schemas, so the super-admin can operate before any UI exists. List the screens the frontend will need later
(template list, draft editor, test-run results, tenant assignment, audit log) as a follow-up in your final report.

Use the existing role and permission system for super-admin. If no super-admin role exists, report that in
Phase 0 instead of inventing one.

## 9. Tests

Build a pytest harness that runs `tests/funnels/<category>.yaml`:
- It drives the **real engine** with mocked tools: tenant-website listing, live lookup, delivery list, `post_order`.
- It uses a **frozen clock** in Asia/Kathmandu and a **mock HTTP receiver** for webhooks (e.g. respx).
- **Hard assertions** check state, cart, tool calls and events. They must pass on every run.
- **Soft assertions** use an LLM judge. Each case runs `runs_per_case` times and must pass at least `pass_threshold`.
- **Global asserts** apply to every case: no re-asked filled slot, at most one confirm prompt, side-effect tools only in
  `submit`, and every number shown equals a `quote_cart` value.
- **Replay:** load anonymised failed transcripts from `fixtures/transcripts/restaurant_failed/*.json`, replay each up
  to the failing turn, and judge only the new next reply.
- The full LLM suite runs in CI only when funnel templates, tools or tests change (Groq rate limits). Unit tests for
  the parser, state machine, quote maths and outbox run on every push.

Restaurant cases (full spec in the YAML):

| ID | Checks |
|---|---|
| R1 | Closing time / last-order cutoff / closed day / ad-hoc "closed today" → checked before any slot question; no `post_order`; pre-order only if allowed |
| R2 | Misspelled items → fuzzy "did you mean"; ambiguous → options in one line; never invent items |
| R3 | Item listed as available but marked unavailable on live lookup at confirm → remove, keep rest, re-quote, re-confirm; site down → don't submit |
| R4 | Location not in website deliverable list but in staff-confirmed list → use staff fee; neither → pickup, keep cart |
| R5 | Every Rs amount equals the `quote_cart` output (qty changes, discounts, delivery fee) |
| R6 | Post-conversation events: fires once, retry without duplicates, dead-letter + owner notify, abandoned → call-back notify |
| R7 | Order changes (add, qty, remove, replace, edit after confirm, digression) → exact final cart |
| R8 | Fulfilment inference: a location, delivery verb (incl. lyaidinu/lyaidena) or pickup verb already given → never asks "delivery or pickup?"; branch + pickup verb → pickup |
| R9 | Staff unreachable / wants a human → order kept, owner notified, handoff with a full summary, no greeting reset |

Planned later: **RE** (real estate: visit-date validation, BS↔AD and relative dates, reschedule, sold listing,
calendar event) and **CC** (content agency: brief capture, pricing-tool quote, deadline feasibility, lead → CRM).
Design the engine and harness so a new category means a new template, new tools and a new YAML file. No engine changes.

## 10. Phases and how to report

**Phase 0 — Discovery (no code changes). Report back with:**
1. Where conversation handling lives today (files and functions), and how the restaurant flow and its website POST work.
2. How the queue, LLM client (Groq/Gemini) and notifications are structured, and what you can reuse.
3. Where the menu and delivery data come from today (website sync? RAG chunks? DB?). Structured data is required for R2–R5.
4. The existing API contracts the frontend uses for conversations. List them; they must not change.
5. Whether roles include super-admin, and whether Alembic is in use.
6. Conflicts with this plan, and your proposed adjustments.

**Phase 1:** Template parser and schema, state store, engine skeleton behind the flag. Unit tests.
**Phase 2:** Restaurant tools (`search_menu`, `get_business_hours`, `lookup_items`, `lookup_delivery`, `quote_cart`,
`post_order` wrapping the existing integration). Staff-confirmed locations table.
**Phase 3:** Outbox, actions worker, delivery log endpoints.
**Phase 4:** Test harness, R1–R7 and replay, plus CI wiring.
**Phase 5:** Super-admin management (section 8): template and version CRUD, behaviour blocks and includes,
validation, test-gated publish, archive with impact check, rollout/rollback, sandbox-chat, tenant assignment,
audit log. The seed command loads `funnels/templates/*.md` as published v1.
**Phase 6:** Switch the restaurant tenant to `funnel`. Watch the "called the shop" / abandoned rate for a week.

After each phase: run the tests, summarise what changed (files plus a one-line reason each), and list anything
deferred. Keep commits small, one phase or sub-step each.

## 11. Definition of done
- The restaurant tenant runs on `funnel` with R1–R7 hard assertions at 100% and soft assertions at 80% or more.
- The legacy path is unchanged for all other tenants. No frontend changes. Existing endpoint contracts are identical.
- Post-conversation actions are configurable per tenant with signed, idempotent, retried delivery and a visible log.
- A new category can be added with only a template, tools and a test YAML.
- The super-admin can, via API alone, create a funnel, edit its Markdown behaviour and behaviour blocks, run
  its tests, publish it, assign it to a tenant, archive it and roll back. No deploy is needed, and conversations
  already in progress are never affected.
