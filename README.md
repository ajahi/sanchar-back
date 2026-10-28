# Sanchar — backend (`nepsocial-backend`)

Sanchar is a multi-tenant inbox and AI sales assistant for Nepali businesses. A business (a **tenant**, also
called a *workspace*) connects its **Instagram**, **Facebook Page (Messenger)** and **WhatsApp**. Customer
messages land in one inbox, and an AI bot answers them from the business's own information. The bot can send
product photos, collect orders (name, phone, address, items), and even place the order in the business's own
website ordering system.

- Backend: this repo, FastAPI. Frontend: `next-app` (Next.js 15, React 19, Tailwind), repo `ajahi/sanchar-front`.
- Live site: https://sanchar.utsavupahar.com. Meta app: "messenger-rag".
- Working branch: **`tst/auto-reply`**. A push to it deploys (see [Deploying](#deploying)).

> **New Claude session?** Read this whole file first. Then read the module docstrings: every important file
> starts with a plain-English explanation of what it does and why. Code comments starting `ponytail:` mark
> deliberate shortcuts and when to upgrade them.

---

## How a customer message flows (the big picture)

```
Customer DMs the business on Instagram / Messenger / WhatsApp
        │
        ▼
Meta → POST /api/v1/webhooks/instagram (or /facebook)        app/api/v1/webhooks.py
        │  signature check → find/create customer → find/create conversation → store message
        │  phone number in the text? → sales lead              app/services/leads.py
        │  answers Meta 200 at once; the reply runs as a background task
        ▼
Auto-reply pipeline                                           app/services/reply_pipeline.py
        │  skip if: AI off for the tenant · a person replied in the last 10 min · no Shop Info
        │  customer sent a photo → hand over to a person (bot does not guess from pixels)
        │  chat came from a mapped ad + asks price → campaign answer (no model)
        ▼
Prompt = rules + Shop Info + photo list + order rules          app/services/auto_reply.py
        + last 6 messages + the new message (+ a "photo hint")
        │  Groq model writes the reply, maybe with hidden labels: [photo:P2]  [order: …]
        ▼
Labels removed → text sent → photos sent → order saved to the lead
        → (optional) order placed in the business's own system  app/services/kitchen.py
```

---

## 1. Channels: Instagram, Messenger, WhatsApp

All three share the same inbox tables (`customers`, `conversations`, `messages`). The per-channel code is in
`app/services/meta/{instagram,facebook,whatsapp}.py`. Each module has the same `send_text(token, account_id,
to, text)` and `send_image(…)`, chosen through `CHANNELS[platform]`, so the bot and the dashboard never care
which channel a chat is on.

| | Connect (`app/api/v1/social_accounts.py`) | Receive | Notes |
|---|---|---|---|
| **Instagram** | Instagram Business Login (OAuth): `/instagram/login → /callback → /finish`. Also creates the workspace and session on first login | `POST /webhooks/instagram` (object `instagram`) | Graph API `v25.0`. Imported posts' image links expire (refreshed every 15 min when the photos panel opens) |
| **Messenger** | Facebook Login: `/facebook/login → /callback`, pick a Page | `POST /webhooks/facebook` (object `page`) | Same event shape as Instagram (`ingest_event`) |
| **WhatsApp** | Embedded Signup popup (`/whatsapp/signup-config`, `/whatsapp/signup`), or manual `POST /whatsapp`, or `scripts/connect_whatsapp.py` | Same webhook URL (object `whatsapp_business_account`) → `ingest_whatsapp_message` | No echoes. Delivery statuses are only logged (e.g. `131047` = outside 24h window) |

**Rules every channel follows**
- **24-hour window:** a business can only reply within 24h of the customer's last message. The bot always
  replies right away, so this only matters for staff replying late.
- **Idempotent:** messages are unique on Meta's id (`external_message_id`). Meta re-sends webhooks; duplicates are ignored.
- **Echoes (Instagram/Messenger):** Meta sends back a copy of every message *the page* sends.
  - An echo of a person typing in the Instagram app is stored as `sender_type='agent'`, which **pauses the bot
    for 10 minutes** (`HUMAN_PAUSE_MINUTES`).
  - The bot's own messages are **stored before sending**. Their echo, which often arrives before Meta's send
    call returns, is matched to that row (`claim_own_echo`), so the bot never pauses itself.
  - Dashboard sends take over an echo row that is already stored instead of inserting a duplicate.
  - If you test from an Instagram account that is *also* connected to Sanchar, that other workspace's bot
    sees your message as "a person replied" and stays quiet. That is correct; use a personal account for tests.
- **Access tokens** are encrypted with `TOKEN_ENCRYPTION_KEY` (Fernet). **Never change that key without a
  migration**: every stored token would become unreadable.
- **Logs:** one line per webhook event, with no message text or customer ids, e.g.
  `instagram event for 1784…: message (text)` or `echo (our side sent a message)`.

---

## 2. The AI bot

- **Model:** Groq OpenAI-compatible API, `LLM_MODEL=openai/gpt-oss-120b` ($0.15 / $0.60 per million tokens),
  `reasoning_effort: low`. A reply costs about 4–7K prompt tokens (~$0.001).
- **CAG, not RAG:** the tenant's whole Shop Info goes into the system prompt. No embeddings. Cap: 30,000
  characters per tenant (`knowledge.MAX_CHARS`). A bigger business would need retrieval (pgvector).
- **Prompt** (`SYSTEM_TEMPLATE` in `auto_reply.py`):
  - answer only from Shop Info, never invent prices
  - reply in the customer's script (Roman Nepali in → Roman Nepali out; one retry if it drifts into Devanagari)
  - short, no markdown
  - works for any kind of business (shop, rental, consultancy, restaurant…)
  - Order of sections matters and was measured: rules → SHOP INFORMATION (+ photo list) → ORDERS rules →
    REMINDER. Putting the order rules before the shop info made the bot attach unrelated photos.
- **Memory:** only the last 6 messages of the same chat (`HISTORY`). The bot does **not** learn from other
  chats. To teach a style, add an example conversation as a Shop Info topic (placeholders, no real customer data).
- **Old replies anchor the model:** after changing the prompt or Shop Info, test from a **new chat**.
- **Switches:** per-tenant `tenants.ai_auto_reply` (Shop Info page, or `PATCH /admin/tenants/{id}/ai`).
- **Test the bot:** `POST /knowledge/test` answers one message from the saved Shop Info, sends nothing, and
  shows `[would send photo: …]` / `[would save order: …]`. It sees no history, so it can't test multi-step flows.

### Shop Info (knowledge)
`app/api/v1/knowledge.py`: one `knowledge_documents` row per topic (title + text), written by the owner.
Starter topic suggestions exist per business type. Tips that proved to matter:
- Write prices **with their unit** ("Momo (per plate)"). Otherwise the bot may assume "per piece".
- The bot **cannot read text inside images**. A menu that exists only as photos must also be in Shop Info as text.

---

## 3. Photos: upload, AI tagging, photos in replies

**Upload and tag** (`app/api/v1/shop_media.py`, frontend `PhotoTaggerModal.tsx`)
- Photos are uploaded (max 5 MB; the browser shrinks them to 1600px JPEG first) to **MinIO/S3**
  (`app/services/storage.py`, public URLs so Meta can fetch them). Instagram posts are also imported automatically.
- Each photo has a **title**, **tags** (≤ 8 short words, `shop_media.tags`, migration `0010`) and an **in stock** flag.
- **Vision model suggests title + tags:** `POST /shop-media/{id}/suggest` → `app/services/vision.py` (Groq,
  `VISION_MODEL`, default `qwen/qwen3.8-27b`). Suggestions reuse the shop's existing tag words. The owner
  confirms them in a slideshow modal (photo left, tags right).
  - Possible upsell: tagging could be a premium feature; plan gating isn't built.

**Photos in replies** (`auto_reply.py`, `reply_pipeline.py`)
- Up to 40 in-stock photos are listed in the prompt as `P1 Name [tags]`. Uploads count once they have a
  name or tags; Instagram posts only once tagged.
- The model appends `[photo:P2]`. The backend strips the label and sends that image after the text, at most 3 picks.
- **Photo hint:** if the customer's words spell out a photo's tag ("bhai tika", "tihar", "menu"), a system note
  points the model at it. This raised hit rate from ~7/8 to 18/18.
- **Sets:** a picked photo grows into its set, up to 10 (`expand_picks`). Photos sharing the tag the customer
  asked for (all pages tagged `menu`), or tagged exactly alike, are sent in upload order. The history gets one
  line, `[sent 5 photos: …]`, so photos don't push the conversation out of the 6-message memory.
- **Customer sends a photo** → the bot does not answer it: a handover notification goes out, plus one holding reply.

---

## 4. Sales funnel: leads → orders

**Leads** (`app/services/leads.py`, `sales_leads` table, one row per customer per tenant)
- Any Nepali mobile number in a customer message becomes a lead, whether or not the bot is on.
  Matched by regex: 97/98 + 8 digits, with or without +977, Devanagari digits too.

**The order conversation** (the "ORDERS AND BOOKINGS" rules in the prompt). When a customer wants to buy or book:
1. **Exact item and quantity.** A general name like "momo" with several kinds/styles in Shop Info → the bot lists
   the options and asks. It never picks one, and keeps an earlier quantity ("duita momo" → "buff jhol" = 2 Buff Jhol Momo).
2. **Name, phone, address** in one message.
3. **Summary + "Yo order thik cha?"**
4. Only after "ho"/"thik cha": "Dhanyabad! Order confirm garna hamro team le chadai call garnecha." plus the hidden
   label `[order: name | phone | address | items]`. The bot never says the order is already confirmed, and never calculates totals.

`split_order` strips the label. A label without a real phone number (≥ 7 digits) is ignored; the model
sometimes copies the template. `save_order` puts `{name, phone, address, items}` in `sales_leads.metadata.order`.
A changed order resets the status to `new` and notifies staff.

**Orders view** (frontend `OrdersModal.tsx`, API `GET /leads?status=`, `PATCH /leads/{id}`): tabs TO CONFIRM /
CONFIRMED / CANCELLED / ALL. Staff call the customer, then mark the order. Statuses are `new | confirmed | cancelled`.

---

## 5. Kitchen integration: placing orders in the business's own website

For The Cube Cafe (thecubeventure.com), with the owner's agreement. Code: `app/services/kitchen.py`, table
`order_integrations` (migration `0011`), API `app/api/v1/kitchen.py` (`/kitchen…`).

- Their site's public API (base `https://admin.thecubeventure.com/api/v1`):
  - `GET product/getall?page=N&limit=40` → menu, grouped by category, 352 items
  - `GET product/getadd` → 64 delivery areas with a charge (the website's dropdown)
  - `POST order/placeorder` → guest checkout. It triggers the owner email + kitchen ticket (KOT) + staff call.
- **Sync** copies the menu and areas into `order_integrations` and writes a Shop Info topic "Menu (synced from
  the website)", so the bot uses exact item names. It re-syncs automatically when the copy is older than 6h at order time.
- **Matching is plain text, no model:** "2 Buff Jhol Momo", "Veg Steam Momo x2" and "duita veg sadeko momo" all
  match. A line that is ambiguous ("chicken momo", "Coke") or unknown is **never guessed**: it waits for staff.
  The address must contain one of the delivery areas (or "pick up").
- **Sending:** after the customer confirms, `auto_send` posts the same JSON the website posts, but only when
  `send_enabled` and `auto_send` are on and every line + the area matched. The result (`kitchen_order_id` =
  their `intradeOrderId`, total, area) is saved in `sales_leads.metadata.kitchen`. A failure is saved as
  `kitchen_error` and notifies staff.
- **Safety:**
  - Sending is off by default.
  - The server only calls hosts listed in `KITCHEN_HOSTS`.
  - The kitchen step runs in its **own DB session**: if it fails, the customer still gets the reply and the order is still saved.
  - **Every send prints a real KOT.** Test orders must be named "SANCHAR TEST - do not prepare" and cancelled by the owner.
- Currently connected: tenant `ajahi_amit` (`93212f76-f8d5-4a77-b58f-f3538795330d`). Test orders #7378 and #7379 were placed manually.
- **Open decision:** the frontend KITCHEN panel (settings + manual send for unmatched orders) is built in
  `next-app` but **not committed**. The owner may prefer backend-only (setup via SQL; see the cheat-sheet below).

---

## 6. Other features (briefly)

- **Campaigns / ads** (`app/services/campaigns.py`, `/campaigns`): a chat that starts from an ad or `ref` link is
  linked to it; staff map ads to a campaign (price, blurb, photos). Price questions in such chats get the
  campaign's answer directly. Campaign photos can be posted to Instagram from Sanchar.
- **Human handover:** a timer, not a mode. Any staff reply (dashboard or Instagram app) pauses the bot for 10
  minutes, refreshed on every reply. "Needs attention" = an open handover nobody has answered.
- **History sync:** "Load older chats" pulls older Instagram/Messenger threads (50 per click).
- **Notifications** (in-app): new chat, lead captured, order received, kitchen send failed, photo handover.
- **Dashboard** (`/dashboard`): per-channel counts. **Platform admin** (`/admin`): all tenants, AI switch.
- **Roles:** owner / admin / agent. Every tenant query filters on `tenant_id` from the session (`app/core/tenant.py`).

---

## Data model (main tables)

`tenants`, `users` (+ `roles`, `permissions`), `social_accounts` (a connected IG/FB/WA account, encrypted token),
`customers`, `conversations`, `messages` (unique `external_message_id`), `handover_events`, `notifications`,
`audit_logs`, `knowledge_documents` (Shop Info), `shop_media` (photos + tags), `campaigns` + `campaign_links`,
`sales_leads` (lead + `metadata.order` + `metadata.kitchen`), `order_integrations`.
Migrations: `app/db/migrations/versions/0001 … 0011`.

---

## Configuration (`.env`, see `.env.example`)

| Group | Variables |
|---|---|
| DB / auth | `DATABASE_URL`, `JWT_SECRET`, `TOKEN_ENCRYPTION_KEY` (never rotate without a migration) |
| Meta | `META_APP_SECRET`, `INSTAGRAM_APP_ID/SECRET/REDIRECT_URI/SCOPES`, `FACEBOOK_APP_ID/REDIRECT_URI`, `*_WEBHOOK_VERIFY_TOKEN`, `WABA_TOKEN`, `WABA_PHONE_NUMBER_ID`, `WHATSAPP_CONFIG_ID` |
| AI | `GROQ_API_KEY`, `LLM_MODEL`, `VISION_MODEL`, `HUMAN_PAUSE_MINUTES` |
| Storage | `S3_ENDPOINT`, `S3_BUCKET`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_PUBLIC_BASE_URL` (MinIO on the VPS, shared server; it still uses default credentials, so change them) |
| Kitchen | `KITCHEN_HOSTS` (allowlist, default `admin.thecubeventure.com`) |
| Mail | `MAIL_*` (email verification) |

---

## Running locally

```bash
python -m venv .venv && . .venv/Scripts/activate && pip install -r requirements.txt
cp .env.example .env                      # fill JWT_SECRET, TOKEN_ENCRYPTION_KEY, GROQ_API_KEY…
docker compose up -d                      # Postgres (the local container maps to port 5435)
alembic upgrade head
uvicorn app.main:app --reload             # http://localhost:8000/docs
```
**Tests:** `DATABASE_URL=postgresql+asyncpg://nepsocial:nepsocial@localhost:5435/nepsocial python -m pytest -q`.
DB tests run inside a rolled-back transaction. **8 tests fail locally because of leftover rows in the dev DB**
(fixed ids like `pn-1`, `m1`): admin, campaigns, fb_ingest, history_sync, ig_media, reply_pipeline::test_reply_pipeline,
wa_ingest, webhook_ingest. Compare against that baseline. Run them with unique ids when touching that code.

**Prompt changes need real-model checks**, not just unit tests. The pattern used so far: a small script that
calls `generate_reply` with a realistic Shop Info and scripted customer turns (feeding the bot's replies back as
history), repeated 4–8 times, counting correct behaviour.

---

## Deploying

- Push to `tst/auto-reply` → the self-hosted GitHub runner on the VPS pulls it into `/home/ajahi/sanchar-back`
  and runs `docker compose up -d --build api` (container `nepsocial-api`, port 8005, DB container `nepsocial-postgres`).
- **Migrations are NOT run by the deploy.** After a push with a new migration:
  `docker exec nepsocial-api alembic upgrade head`. Forgetting this caused a "relation does not exist" crash once.
- Image is slim (~364 MB): allowlist `.dockerignore`, pip removed in the image.

---

## Working rules (for people and for Claude)

- **Never commit or push for the owner.** They push and deploy themselves.
- In learning/discussion threads, **explain first; edit only after an explicit go-ahead**.
- Never show server logs on screen recordings (they can contain secrets). Never paste password hashes.
- Secrets that leaked earlier and must be rotated: `META_APP_SECRET`, `WABA_TOKEN`, `INSTAGRAM_APP_SECRET`, `GROQ_API_KEY`, `MAIL_PASSWORD`.
- The Meta App Review screencasts are submitted. Changes so far did not alter the reviewed flows (login, connect,
  inbox, reply). Keep it that way, or a new screencast is needed.
- Don't run `alembic downgrade` on the server.
- Anything that sends to an outside system (Meta, the café's kitchen) is a real action: confirm before testing.

---

## Debug cheat-sheet

```sh
docker logs nepsocial-api --since 10m 2>&1 | grep -iE "event for|auto-reply|LLM reply|kitchen"
docker exec nepsocial-api alembic current
docker exec nepsocial-api grep -c "Yo order thik cha" app/services/auto_reply.py   # is the new prompt deployed?
```
- `auto-reply skipped: a person is handling…` → a staff reply (or a test from another connected account) in the last 10 min.
- `auto-reply skipped: AI is off…` / `…no shop context yet` → tenant switch / empty Shop Info.

Orders sent to the café (from the server):
```sql
select l.updated_at, l.meta->'order'->>'name' name, l.meta->'order'->>'items' items,
       l.meta->'kitchen'->>'kitchen_order_id' cafe_order_no, l.meta->>'kitchen_error' error
from sales_leads l where l.tenant_id = '<TENANT_ID>' and l.meta ? 'order' order by l.updated_at desc limit 10;
```
Kitchen setup without the UI: `insert into order_integrations (tenant_id, base_url, email) values (…)`, then run
`kitchen.sync` (see `app/services/kitchen.py`), then `update order_integrations set send_enabled=true, auto_send=true …`.

---

## Known limits and next ideas

- One open order per customer (a newer order replaces the old one). No order history table.
- The bot can't read text in images. A "Read menu photo into Shop Info" button (vision model) was proposed.
- A "Use this chat as an example" button (copy a staff conversation into Shop Info) was proposed.
- WhatsApp *coexistence* (business app + API on one number) needs a different signup `featureType` + `smb_app_data` sync. Not built.
- Background work uses FastAPI BackgroundTasks: a restart loses in-flight replies. Use a jobs table if that matters.
- Kitchen client speaks one storefront API (thecubeventure.com's). Another site needs its own client.
