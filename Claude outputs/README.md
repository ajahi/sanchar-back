# Sanchar — Conversation Funnels

Funnels define how Sanchar's agent handles one kind of business conversation (restaurant order,
property visit, agency brief, …). Each funnel is a Markdown file with YAML frontmatter:

- **Frontmatter (enforced by code):** slots, states, which tools each state may use, guards, post-conversation actions.
- **Markdown body (read by the LLM):** tone, "don't be annoying" rules, and real example conversations.

The LLM handles language. Code owns state, prices, availability, transitions and side-effects.

---

## How it fits together

```
Instagram / Messenger / WhatsApp webhook
        │
        ▼
 Router ──► picks the tenant's active funnel(s) by trigger description
        │
        ▼
 Funnel engine
   ├─ slot extraction (fills every slot it can from each message)
   ├─ state machine (from frontmatter) ── scopes tools per state
   ├─ cart / slot store (Postgres, per tenant + conversation)
   └─ LLM reply (Markdown body + injected state summary)
        │
        ▼
 Outbox events ──► worker ──► tenant actions (http_post, webhook, notify, app update)
                                └─ retries, idempotency key, HMAC signature, delivery log
```

### Templates and tenants

| Table | Owner | Purpose |
|---|---|---|
| `funnel_templates` | Super-admin | One per category + version (`restaurant@3`). Holds the Markdown spec and status `draft → testing → published`. |
| `funnel_test_suites` | Super-admin | Test cases per category (R*, RE*, CC*). A template version can only be published if its suite passes. |
| `tenant_funnels` | Tenant | Which published template(s) the tenant uses, pinned to a version, plus overrides (hours, menu source, delivery zones, tone, examples, actions). |
| `tool_registry` | Code | Tools exist in code; templates reference them by name; tenants supply endpoints/credentials. |

A tenant picks a funnel from the dashboard, fills in its overrides and turns it on.
Conversations already in progress stay on the engine/version they started with.

### Super-admin controls

The super-admin can manage funnels at runtime, with no deploy:

- **Add** a funnel template (upload `.md`), or add reusable **behaviour blocks** (global rules, Nepglish style,
  escalation rules) that templates pull in with `includes:`.
- **Edit** Markdown behaviour, examples or includes. Each edit creates a new draft version; published versions never change.
- **Test and publish**: drafts are validated, the category test suite runs, and a draft is published only if it passes.
  `sandbox-chat` lets you try a draft with mocked tools and no side effects.
- **Assign or unassign** funnels per tenant and pin versions; roll out a new version or **roll back** to an earlier one.
- **Remove**: archive a template or block. You see which tenants it affects first. Their conversations already in
  progress finish normally, and new ones move to the replacement version or the legacy engine. Only never-published
  drafts can be hard-deleted.
- Every change is recorded in the **audit log**.

---

## Test cases

Every category has a test suite. Each suite runs against a fixture tenant with mocked tools,
a frozen clock and a mock webhook receiver.

- **Hard assertions** check state, tool calls and events. They are enforced by code guards and must pass on **every** run.
- **Soft assertions** check wording and tone with an LLM judge. Each case runs 5 times and needs at least 80% to pass.

### Rules for every category (the "don't be annoying" contract)

| Check | Type |
|---|---|
| Never re-asks a slot that is already filled | hard |
| At most one confirmation prompt (one more only if the cart changes after confirm) | hard |
| Side-effect tools (e.g. `post_order`) only callable in the `submit` state | hard |
| Never greets again after the first message; an open order is never dropped | hard |
| Max 3 lines per bot message | soft |
| Replies in the customer's language (Nepglish in → Nepglish out) | soft |
| Never copies example text from the template ("e.g., chipledhunga…") | soft |
| Judge: nothing repetitive or unnecessary for a busy customer | soft |

### R — Restaurant / Café

Spec: [`tests/funnels/restaurant.yaml`](tests/funnels/restaurant.yaml)

| ID | What it checks | Cases | Pass condition |
|---|---|---|---|
| **R1** | Closing time and ad-hoc closures | R1a order after last-order cutoff · R1b closed day, schedule for tomorrow · R1c "closed today" closure · R1d closure with pre-order allowed · R1e closure notice only in the KB | Store status is checked by code **before** any slot question; when closed, no delivery/pickup/address questions, only the closure message (plus pre-order if allowed); `post_order` never called |
| **R2** | Misspelled items ("did you mean…?") | R2a single confident match · R2b ambiguous → options in one line · R2c Devanagari/Romanized · R2d unknown item | Fuzzy `search_menu` used; nothing added to cart until confirmed; never invents an item or price |
| **R3** | Item listed as available but marked not available at confirm | R3a one item unavailable on live lookup · R3b all items unavailable · R3c price changed since listing · R3d tenant website lookup fails | Live `lookup_items` at confirm always wins; unavailable items removed, rest kept, total re-quoted and re-confirmed; never submits an empty or unverified order |
| **R4** | Location not in the website's deliverable list, but confirmed by staff | R4a in staff-confirmed list · R4b in neither list · R4c misspelled staff-confirmed location | `lookup_delivery` checks website list, then staff-confirmed locations (dashboard) with their own fee; neither → offer pickup, cart kept |
| **R5** | Calculation mistakes | R5a multi-item + delivery · R5b qty change + discount code | Totals only come from `quote_cart`; every Rs amount in the reply matches the quote |
| **R6** | Post-conversation events | R6a webhook fires once · R6b 500→500→200 retry · R6c receiver down · R6d abandoned conversation | Exactly one accepted delivery (idempotency key); HMAC valid; dead-letter + owner notified on failure; customer never told "success" falsely; abandoned cart → call-back notification |
| **R9** | Staff unreachable / wants a human | R9a "phone ta uthyna ta" mid-order · R9b "phone uthaunus na" · R9c operator gets the full summary | Order kept; owner notified with cart + slots; handoff payload lets the operator continue without re-asking; never resets to a greeting |
| **R8** | Delivery/pickup inference | R8a location given · R8b "deliver to" in English · R8c location in a later message · R8d pickup phrasing · R8e branch name + pickup · R8f changes mind | Never asks "delivery or pickup?" when a location, a delivery verb or a pickup verb was already given; a branch name + pickup verb means pickup, not an address |
| **R7** | Tracking changes to the order | R7a add / change qty / remove / replace · R7b edit after confirm · R7c digression mid-order | Final cart exactly matches; edit after confirm → one re-confirm before `post_order`; cart survives off-topic questions |

**Confirm-time lookup:** items are listed from the tenant website during the conversation. At confirm,
the engine looks up each item live (availability + price) and the delivery location (website list,
then staff-confirmed list), builds the quote with delivery cost, and only then shows the total.

### RE — Real estate (visit booking) · *planned*

| ID | What it checks |
|---|---|
| RE1 | Visit date in the past, on a holiday, or when the agent is unavailable |
| RE2 | Relative and Nepali dates ("next Saturday", "Mangsir 5", BS ↔ AD) resolve correctly |
| RE3 | Reschedule and cancel without losing property/contact details |
| RE4 | Property already sold or rented → offer similar listings |
| RE5 | Calendar event + reminder sent via post-conversation actions, exactly once |

### CC — Content creation agency · *planned*

| ID | What it checks |
|---|---|
| CC1 | Brief collection (platform, deliverables, audience) without one-question-at-a-time |
| CC2 | Package/quote comes only from the pricing tool |
| CC3 | Deadline feasibility against the agency's capacity |
| CC4 | Reference links/files captured and attached to the lead |
| CC5 | Qualified lead → CRM webhook + owner notification |

### Real-transcript replay

Failed production conversations (anonymised) live in `fixtures/transcripts/<category>_failed/`.
Each one is replayed up to the turn where it went wrong. Only the **next** reply from the new
funnel is judged: is it shorter, does it avoid re-asking, and does it move the order forward?

---

## Running the tests

```bash
# one category
pytest tests/funnels -k restaurant

# one case, verbose transcript
pytest tests/funnels -k R6b -s

# replay real failed conversations
pytest tests/funnels -k replay
```

CI runs the suite for any category whose funnel template, tools or tests changed. Publishing a
template version from the super-admin panel runs the same suite and blocks on failure.

---

## Adding a new business category

1. Write the template: `funnels/<category>.md` (frontmatter + tone + real examples).
2. Add any new tools to `tool_registry` (code).
3. Write `tests/funnels/<category>.yaml` with a 2–3 letter prefix (e.g. `CL` for clinics).
4. Add the category's row to the tables above.
5. Upload as `draft` in super-admin → suite runs → `published` → tenants can select it.
