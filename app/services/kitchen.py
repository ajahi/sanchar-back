"""Send confirmed chat orders into a business's own online-order system: the same request its website checkout
makes (thecubeventure.com's guest checkout), so its usual flow runs: owner email, kitchen ticket, staff call.

The site's menu and delivery areas are synced into order_integrations and written to a Shop Info topic, so the
bot quotes the exact item names and prices. The order the bot collected (free text, "2 Buff Jhol Momo") is then
matched against that menu as plain text, no model: an exact name (any word order), or a near spelling
("sadeko" / "sadheko"). A line that matches nothing, or two items equally well, is left for staff to pick:
an unmatched line is never guessed into the kitchen.

ponytail: one storefront API (the one thecubeventure.com uses); another site needs its own client here.
"""
import difflib
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.knowledge import KnowledgeDocument
from app.models.order_integration import OrderIntegration
from app.models.sales_lead import SalesLead
from app.services.http_client import SSL_CTX
from app.services.notifications import create_notification

log = logging.getLogger(__name__)

SYNC_EVERY = timedelta(hours=6)  # menu and delivery charges older than this are re-read before an order is built
PICKUP = "Pick up"
MENU_TITLE = "Menu (synced from the website)"
_HEADERS = {"User-Agent": "Sanchar/1.0 (+https://sanchar.utsavupahar.com)"}

_NUM_WORDS = {"ek": 1, "euta": 1, "one": 1, "dui": 2, "duita": 2, "two": 2, "tin": 3, "tinta": 3, "three": 3,
              "char": 4, "charta": 4, "four": 4, "panch": 5, "panchta": 5, "five": 5}
_UNITS = {"plate", "plates", "pcs", "pc", "piece", "pieces", "cup", "cups", "glass", "glasses", "bottle", "bottles",
          "x", "ta", "wata", "ota", "each", "per", "set", "sets", "rs"}
_LINE_SPLIT = re.compile(r"\s*(?:[,;\n]|\band\b|\bra\b)\s*", re.IGNORECASE)  # names hold "&" and "/", never these


def allowed_url(url: str) -> bool:
    """Only https on a host in KITCHEN_HOSTS: the server calls this URL, so it is never free text."""
    u = urlparse(url)
    hosts = {h.strip().lower() for h in settings.kitchen_hosts.split(",") if h.strip()}
    return u.scheme == "https" and (u.hostname or "").lower() in hosts


def _norm(s: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", s.lower().replace("mo:mo", "momo")))


async def sync(db: AsyncSession, integ: OrderIntegration) -> None:
    """Re-read the site's menu and delivery areas, and rewrite the Shop Info menu topic. Raises httpx.HTTPError."""
    base = integ.base_url.rstrip("/")
    items: dict[str, dict] = {}
    async with httpx.AsyncClient(timeout=20, verify=SSL_CTX, headers=_HEADERS) as client:
        for page in range(1, 50):  # products come grouped by category, 40 categories a page
            resp = await client.get(f"{base}/product/getall", params={"page": page, "limit": 40})
            resp.raise_for_status()
            groups = resp.json().get("data") or []
            if not groups:
                break
            for g in groups:
                for p in g.get("products") or []:
                    items[p["_id"]] = p
        resp = await client.get(f"{base}/product/getadd")
        resp.raise_for_status()
        areas = resp.json().get("data") or []
    integ.catalog = list(items.values())
    integ.locations = [{"address": a["address"], "rate": int(a.get("rate") or 0)} for a in areas if a.get("address")]
    integ.synced_at = datetime.now(timezone.utc)
    await _write_menu(db, integ)


def menu_text(integ: OrderIntegration) -> str:
    by_cat: dict[str, list[str]] = {}
    for p in integ.catalog:
        by_cat.setdefault(str(p.get("category") or "Other").strip(), []).append(f"{p['name'].strip()} - Rs {p['price']}")
    areas = ", ".join(f"{a['address']} Rs {a['rate']}" for a in integ.locations)
    return (
        "The full menu, from our website (updated automatically). Use these exact item names when taking an order.\n"
        + "\n".join(f"{cat}: " + "; ".join(lines) for cat, lines in by_cat.items())
        + (f"\nDelivery areas and delivery charge: {areas}. Pick up from the cafe is free. When taking a delivery "
           "order, ask which of these areas they are in and write that area in the address. If their place is not "
           "one of these areas, never say we don't deliver there: say it is not in our listed delivery areas, so we "
           "don't know its delivery charge yet, and that our staff will call them shortly about the place and the "
           "delivery. Then carry on taking the order, with the place they gave as the address." if areas else "")
    )


async def _write_menu(db: AsyncSession, integ: OrderIntegration) -> None:
    doc = await db.get(KnowledgeDocument, integ.knowledge_id) if integ.knowledge_id else None
    if doc is None:
        doc = KnowledgeDocument(tenant_id=integ.tenant_id, title=MENU_TITLE, document_type="topic", meta={"source": "kitchen"})
        db.add(doc)
        await db.flush()
        integ.knowledge_id = doc.id
    doc.content, doc.status = menu_text(integ), "active"


async def fresh(db: AsyncSession, integ: OrderIntegration) -> None:
    """Sync if the copy is old; on failure keep using it (an hour-old price beats no order)."""
    if integ.synced_at and datetime.now(timezone.utc) - integ.synced_at < SYNC_EVERY:
        return
    try:
        await sync(db, integ)
    except httpx.HTTPError:
        log.warning("kitchen menu sync failed for tenant %s", integ.tenant_id, exc_info=True)


def _qty(word: str) -> int | None:
    """A count: "2", "x2", "2x" (the bot writes "Veg Steam Momo x1"), "duita"."""
    m = re.fullmatch(r"x?(\d{1,2})x?", word)
    return (int(m.group(1)) or None) if m else _NUM_WORDS.get(word)


def match_line(text: str, catalog: list[dict]) -> tuple[dict | None, int]:
    """(menu item or None, quantity) for one order line like "2 Buff Jhol Momo - Rs 250 each"."""
    words = _norm(re.sub(r"rs\.?\s*[\d,]+", " ", text, flags=re.IGNORECASE)).split()
    count = lambda ws: next((q for w in ws if (q := _qty(w))), 1)  # noqa: E731
    # A menu name written out in full, the rest only a count or unit. First, because a name can itself look
    # like a count ("Ramyun 2x Spicy" is an item, not 2 x "Ramyun Spicy").
    for p in sorted(catalog, key=lambda p: -len(p["name"])):
        n = _norm(p["name"]).split()
        for i in range(len(words) - len(n) + 1):
            rest = words[:i] + words[i + len(n):]
            if words[i:i + len(n)] == n and all(_qty(w) or w in _UNITS for w in rest):
                return p, count(rest)
    qty = count(words)
    name = [w for w in words if not _qty(w) and w not in _UNITS]
    if not name:
        return None, qty
    same = [p for p in catalog if sorted(_norm(p["name"]).split()) == sorted(name)]  # exact, any word order
    if len(same) == 1:
        return same[0], qty
    wanted = " ".join(name)
    scored = sorted(((difflib.SequenceMatcher(None, wanted, _norm(p["name"])).ratio(), i) for i, p in enumerate(catalog)), reverse=True)
    if scored and scored[0][0] >= 0.9 and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.04):
        return catalog[scored[0][1]], qty
    return None, qty


def match_location(address: str, locations: list[dict]) -> dict | None:
    """The delivery area named in the address (exact, or a near spelling), {"address": PICKUP, "rate": 0} for a
    pick-up, or None: staff pick it from the same list the website's dropdown shows."""
    a = _norm(address)
    if re.search(r"\bpick ?up\b", a):
        return {"address": PICKUP, "rate": 0}
    hits = [loc for loc in locations if f" {_norm(loc['address'])} " in f" {a} "]
    if hits:
        return max(hits, key=lambda loc: len(loc["address"]))
    words, best = a.split(), (0.0, None)
    for loc in locations:
        n = _norm(loc["address"])
        k = len(n.split())
        for i in range(len(words) - k + 1):
            r = difflib.SequenceMatcher(None, n, " ".join(words[i:i + k])).ratio()
            if r >= 0.88 and r > best[0]:
                best = (r, loc)
    return best[1]


def unlisted(address: str, locations: list[dict]) -> bool:
    """A delivery to a place not in the business's delivery areas: still taken, staff call about the place and charge."""
    return bool(locations) and match_location(address, locations) is None


def area_hint(text: str, locations: list[dict]) -> str:
    """A note for the model naming the delivery area the customer's message spells out ("srijana chowk" ->
    sirjana chowk). Alone, the model misses a near spelling among 60-odd areas, then keeps refusing it: a mild
    note lost to its own earlier "chhaina" in the history (0/4 against the real model), this wording won 4/4."""
    loc = match_location(text, locations)
    if loc is None or loc["address"] == PICKUP:
        return ""
    return (
        f"Note from the business, not the customer: the customer's message names our delivery area "
        f"'{loc['address']}' (delivery charge Rs {loc['rate']}); they may spell it differently. We DO deliver there. "
        "If an earlier reply in this chat said we don't, that reply was wrong: say sorry for the mistake and carry "
        "on with the order."
    )


# Delivery or pick up, as the customer already said it: "Expert ma 2nd floor ma 2 ota dry mix lyaidinu na" is a
# delivery, "7 baje aaera lanchu" a pick up. The model asked "Delivery ki pickup?" anyway, twice in one real chat,
# and the customer phoned the shop. Code reads it (fulfilment), tells the model (fulfilment_hint) and takes the
# question out of a reply that still asks it (drop_fulfilment_question).
# ponytail: word lists, English and Romanized Nepali; the latest message that says one wins (they changed their mind).
_PICKUP_SAID = re.compile(
    r"\bpick\s*-?\s*up\b|\btake\s*-?\s*away\b|\blina\s+(aau|au|aaun|aaunchu|auchu|aauchu|aaucha|aunchu)|"
    r"\b(aaera|aayera|ayera|aera)\s+lan|\baafai\s+lin|\baafai\s+aau|\bparcel\s+lin",
    re.IGNORECASE,
)
_DELIVERY_SAID = re.compile(
    r"\bdeliver(y|ed)?\b|\bbring\b|\blyai?d[ie]n|\blyau(nu|nus|na)\b|\bpathai?d[ie]n|"
    r"\bpathau(nu|nus|na)\b|\bghar\s+(samma|ma)\b|ल्याइदि|पठाइदि",
    re.IGNORECASE,
)


def fulfilment(customer_texts: list[str], locations: list[dict]) -> tuple[str, str] | None:
    """("delivery" | "pickup", the customer's words that said it) from their messages, oldest first; None if they
    have not said. Naming one of our delivery areas counts as delivery."""
    found = None
    for text in customer_texts:
        if _PICKUP_SAID.search(text):
            found = ("pickup", text)
        elif _DELIVERY_SAID.search(text) or ((loc := match_location(text, locations)) and loc["address"] != PICKUP):
            found = ("delivery", text)
    return found


def fulfilment_hint(said: tuple[str, str] | None) -> str:
    if said is None:
        return ""
    how, text = said
    quote = text.strip().replace('"', "'")[:120]
    if how == "pickup":
        return (f'Note from the business, not the customer: they already said they will PICK UP ("{quote}"). Never '
                "ask delivery or pick up, and don't ask for a delivery area or address: write \"Pick up\".")
    return (f'Note from the business, not the customer: they already said they want DELIVERY ("{quote}"). Never ask '
            "delivery or pick up. If they named a place, that is their address: don't ask for it again. Ask only "
            "for what is still missing (the delivery area only if their place is not one of our areas).")


_CHOICE = re.compile(
    r"[^.!?\n]*\b(delivery\b[^.!?\n]*\bpick\s*-?\s*up|pick\s*-?\s*up\b[^.!?\n]*\bdelivery)\b[^.!?\n]*\?[ \t]*",
    re.IGNORECASE,
)
_IF_PICKUP = re.compile(r"[ \t]*\b(if\s+(you\s+want\s+)?pick\s*-?\s*up|pick\s*-?\s*up\s+ho\s+bhane)\b[^.!?\n]*[.!?]?",
                        re.IGNORECASE)
_IF_DELIVERY = re.compile(r"\b(if\s+(it'?s\s+)?delivery|delivery\s+ho\s+bhane)\s*,?\s*", re.IGNORECASE)


def drop_fulfilment_question(reply: str) -> str:
    """A reply without "Delivery ki pickup?" and its "If pickup, ..." for a customer who already said which.
    "If delivery, please tell ..." keeps its ask without the "If delivery, "."""
    out = _IF_DELIVERY.sub("", _IF_PICKUP.sub(" ", _CHOICE.sub(" ", reply)))
    if out == reply:
        return reply
    out = re.sub(r"[ \t]+\n", "\n", re.sub(r"[ \t]{2,}", " ", out))
    out = re.sub(r"(^|[.!?][ \t]+|\n)([a-z])", lambda m: m.group(1) + m.group(2).upper(), out)  # sentence starts
    return re.sub(r"\n{3,}", "\n\n", out).strip() or reply


# Customers write menu words the way they say them: "sorma", "sworma", "shwarma" (Shawarma), "chaumin" (Chowmein),
# "lafing" (Laphing). The model can't hear that (it sees other word pieces: it took "sorma" for shorba, soup), and in
# a 300-item menu it names one match, not all of them. So code finds the items (item_hint) and the model only words
# the reply. A word is matched by its consonants: vowels, y, w and h dropped, ph = f, a double letter once:
# shawarma, sworma, suorma -> "srm". Spellings that misses (sandheko, kothe, hookah) the owner writes into Shop
# Info after the name, in braces: "Chicken Shawarma {sorma, sworma}". Code reads them (spellings) and takes them
# out of the text the model sees (strip_spellings), so the model never repeats them to a customer.
# ponytail: consonant match on single words; a menu word of 3+ consonants only, so short ones (momo) need braces.
_SPELLING = re.compile(r"[ \t]*\{([^{}\n]{1,200})\}")
MAX_LISTED = 6  # more items than this for one word: name the menu's word, not every item
# Chat words that sound like a menu word ("garnu" ~ green). Only checked for words that are not on the menu.
_CHAT_WORDS = set(
    "garnu garne garna garnus garera garchu gardinu gardinus gardiu painxa paincha paucha pauxa milcha milxa "
    "chahiyo chainxa chaincha chahincha pathaunu pathaunus pathaidinu pathaidinus lyaidinu lyaidinus lyaidena "
    "lyaunu dinus dinuhos sakincha sakinxa hunxa huncha bhayo bhaye bhanda bhannu bhannus kasto kasari kahile "
    "tapai hajur malai hamro ahile bholi khulla banda dhanyabad namaste please thanks thank order price rate menu "
    "delivery deliver pickup address location number phone available sasto mitho dherai thorai ramro sanga samma "
    "lagi wala wata plate plates khana".split()
)
# Words that describe a dish rather than name it: on the menu, but "spicy kam garnus" is not asking for Spicy Tuna
# Rice. A word written right is listed only when it is not one of these (sound matches are always dish words).
_DESCRIBING = set(
    "spicy cold hot iced fresh special classic extra plain simple crispy crunchy grilled fried boiled steam big small "
    "double dbl sweet sour green healthy creamy cheesy mixed mix chilly masala hakka keema".split()
)


def sound(word: str) -> str:
    """A word by its consonants: "shawarma", "sworma", "suorma" -> "srm"; "chaumin", "chowmein" -> "cmn"."""
    w = re.sub(r"[^a-z]", "", word.lower()).replace("ph", "f").replace("h", "")
    return re.sub(r"(.)\1+", r"\1", re.sub(r"[aeiouyw]", "", w))


def spellings(text: str) -> dict[str, str]:
    """The owner's spellings in Shop Info, {spelling: name}: "Chicken Shawarma {sorma, sworma} - Rs 350" ->
    {"sorma": "Chicken Shawarma", "sworma": "Chicken Shawarma"}. The name is what stands before the braces on
    that line, after the last ";" or ":"."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        for m in _SPELLING.finditer(line):
            name = re.sub(r"rs\.?\s*[\d,]+", " ", re.split(r"[;:]", line[: m.start()])[-1], flags=re.IGNORECASE)
            name = name.strip(" -,")
            for alias in m.group(1).split(","):
                if name and (a := _norm(alias)):
                    out[a] = name
    return out


def strip_spellings(text: str) -> str:
    """Shop Info as the model sees it: the {spellings} taken out."""
    return _SPELLING.sub("", text)


def _shown(p: dict) -> str:
    cat = str(p.get("category") or "").strip()
    return f"{p['name'].strip()} (Rs {p['price']}{', ' + cat if cat else ''})"


def item_hint(text: str, catalog: list[dict], spelled: dict[str, str] | None = None) -> str:
    """A note for the model naming, by the menu's own names, every item a word of the customer's message points
    to: "sorma painxa?" -> Chicken Shawarma (Rs 350, Wrap) and Shawarma Chicken Rice (Rs 450, Rice). Another word
    of theirs narrows it ("sorma rice" -> the rice). A word written right that is in 2-6 item names gets them
    listed too (the model names one); in one item or in many, it gets nothing (the menu text is enough)."""
    words = _norm(text).split()
    spelled = spelled or {}
    if not words or not (catalog or spelled):
        return ""
    names = [(p, _norm(p["name"]).split()) for p in catalog]
    menu_words = {w for _, ns in names for w in ns}
    by_sound: dict[str, set[str]] = {}
    for w in menu_words:
        if len(w) >= 4 and len(s := sound(w)) >= 3:
            by_sound.setdefault(s, set()).add(w)
    notes, listed = [], set()
    for w in dict.fromkeys(words):
        if w in spelled:
            want = _norm(spelled[w]).split()
            items = [p for p, ns in names if all(x in ns for x in want)]
            label, how = spelled[w], "spelled"
        elif w in menu_words and w.isalpha() and len(w) >= 3 and w not in _CHAT_WORDS | _DESCRIBING:
            items, label, how = [p for p, ns in names if w in ns], w, "exact"
        elif w not in menu_words and len(w) >= 4 and w not in _CHAT_WORDS and len(s := sound(w)) >= 3 and s in by_sound:
            keys = by_sound[s]
            items, label, how = [p for p, ns in names if keys & set(ns)], sorted(keys)[0], "sound"
        else:
            continue
        # their other words that are in some of these names or categories narrow it down ("sorma rice", "sorma
        # wrap": the wrap is "Chicken Shawarma" under Wrap; "chicken dry mix")
        ns_of = {id(p): ns + _norm(str(p.get("category") or "")).split() for p, ns in names}
        others = [x for x in words if x != w and any(x in ns_of[id(p)] for p in items)]
        narrowed = [p for p in items if all(x in ns_of[id(p)] for x in others)]
        items = [p for p in (narrowed or items) if p["_id"] not in listed]
        if how == "exact" and not 2 <= len(items) <= MAX_LISTED:
            continue
        menu_word = next((t for p in items for t in re.findall(r"[\w:']+", p["name"]) if _norm(t) == label), label)
        if not items:  # a spelling of something that is not on the website menu
            notes.append(f"the customer's '{w}' means '{spelled.get(w, menu_word)}'.")
        elif len(items) > MAX_LISTED:
            notes.append(f"the customer's '{w}' is what our menu writes '{menu_word}' ({len(items)} items).")
        else:
            listed |= {p["_id"] for p in items}
            what = "matches" if how != "exact" else "is in the name of"
            notes.append(f"the customer's '{w}' {what} " + ("this menu item: " if len(items) == 1 else
                         f"these {len(items)} menu items: ") + "; ".join(_shown(p) for p in items) + ".")
        if len(notes) == 3:
            break
    if not notes:
        return ""
    return (
        "Note from the business, not the customer: " + " ".join(notes) + " If they are asking about these, use "
        "exactly these menu names, never the customer's spelling, and name EVERY item listed with its price; if "
        "more than one is listed and their message doesn't say which, ask which one they want."
    )


def plan(order: dict, integ: OrderIntegration) -> dict:
    """The lines and area Sanchar would send for this order, before anyone edits them."""
    lines = []
    for text in [t for t in _LINE_SPLIT.split(order["items"]) if t.strip()]:
        item, qty = match_line(text, integ.catalog)
        lines.append({"text": text.strip(), "product_id": item["_id"] if item else None, "quantity": qty})
    loc = match_location(order["address"], integ.locations)
    return {"lines": lines, "location": loc["address"] if loc else None}


def build_body(order: dict, lines: list[tuple[dict, int]], location: dict, email: str | None) -> dict:
    """What the website's guest checkout posts (its Billing page), for these menu items and delivery area."""
    pickup = location["address"] == PICKUP
    charge = 0 if pickup else int(location["rate"])
    phone = re.sub(r"\D", "", order["phone"])
    phone = phone[3:] if phone.startswith("977") and len(phone) == 13 else phone  # the site takes 98XXXXXXXX
    requests = order.get("requests")
    if location.get("unlisted"):  # charge 0 and the customer's own place: staff confirm both on the call
        note = f"DELIVERY AREA NOT IN OUR LIST ({location['address']}): call the customer to confirm the place and delivery charge."
        requests = f"{note} {requests}" if requests else note
    return {
        "data": {"fullName": order["name"], "address": order["address"], "phone": phone, "email": email or "",
                 "deliveryLocation": "Pick-up" if pickup else location["address"]},
        # the site has no note for the whole order, only one per item (its "special requests" box): on every line,
        # so whoever makes each item sees it
        "item": [{**p, "quantity": q, "selectedSize": None, "selectedColor": None, "requests": requests}
                 for p, q in lines],
        "product": [p["_id"] for p, _ in lines],
        "deliveryOption": PICKUP if pickup else location["address"],
        "totalAmount": sum(int(p["price"]) * q for p, q in lines) + charge,
        "deliveryCharge": charge,
    }


def resolve(
    integ: OrderIntegration, picks: list[tuple[str, int]], location: str, allow_unlisted: bool = False
) -> tuple[list[tuple[dict, int]], dict]:
    """Menu items and area for ids / an area name chosen by staff (or by plan). ValueError if one is not on the menu.
    allow_unlisted: a place not in the delivery areas is kept as the customer wrote it, charge 0 (staff call)."""
    by_id = {p["_id"]: p for p in integ.catalog}
    if not picks or any(pid not in by_id or not 0 < q < 100 for pid, q in picks):
        raise ValueError("Pick an item from the menu, with a quantity, for every line.")
    if location == PICKUP:
        loc = {"address": PICKUP, "rate": 0}
    else:
        loc = next((a for a in integ.locations if a["address"] == location), None)
        if loc is None and allow_unlisted and location.strip():
            loc = {"address": location.strip()[:200], "rate": 0, "unlisted": True}
        if loc is None:
            raise ValueError("Pick the delivery area from the list.")
    return [(by_id[pid], q) for pid, q in picks], loc


async def send(db: AsyncSession, integ: OrderIntegration, lead: SalesLead, body: dict) -> dict:
    """Place the order on the site; record it on the lead. Raises httpx.HTTPError (or ValueError if the site
    answers without success). Commits before the network call (no DB connection held while it runs)."""
    await db.commit()
    url = f"{integ.base_url.rstrip('/')}/order/placeorder"
    log.info("kitchen POST %s for lead %s: %s, area %s, total %s, request %s", url, lead.id,
             [(i["name"], i["quantity"]) for i in body["item"]], body["deliveryOption"], body["totalAmount"],
             body["item"][0]["requests"])
    async with httpx.AsyncClient(timeout=20, verify=SSL_CTX, headers=_HEADERS) as client:
        resp = await client.post(url, json=body)
        log.info("kitchen POST for lead %s answered %s: %s", lead.id, resp.status_code, resp.text[:300])
        resp.raise_for_status()
        answer = resp.json()
    if not answer.get("success"):
        raise ValueError(str(answer.get("message") or "The order system did not accept the order")[:300])
    data = answer.get("data") or {}
    sent = {
        "order": lead.meta.get("order"),  # which collected order this was: a changed order may be sent again
        "site_order_id": data.get("_id"),
        "kitchen_order_id": data.get("intradeOrderId"),
        "total": body["totalAmount"],
        "area": body["deliveryOption"],
        "lines": [[i["name"], i["quantity"], i["price"]] for i in body["item"]],
        "at": datetime.now(timezone.utc).isoformat(),
    }
    lead.meta = {**{k: v for k, v in (lead.meta or {}).items() if k != "kitchen_error"}, "kitchen": sent}
    return sent


def already_sent(lead: SalesLead) -> bool:
    k = (lead.meta or {}).get("kitchen")
    return bool(k) and k.get("order") == (lead.meta or {}).get("order")


async def auto_send(db: AsyncSession, lead: SalesLead) -> str:
    """Right after the customer confirmed: send it if this business turned sending and auto-send on, and every
    line and the area matched. Anything else waits in Orders for staff, who are pinged. Never raises.
    Returns "sent", "needs_staff" (not matched, or the site refused it) or "off" (not set up or not auto)."""
    integ = await db.get(OrderIntegration, lead.tenant_id)
    order = (lead.meta or {}).get("order")
    if integ is None or not (integ.send_enabled and integ.auto_send) or not order or already_sent(lead):
        log.info("kitchen auto-send not run for lead %s (tenant %s): set up %s, send %s, auto %s, already sent %s",
                 lead.id, lead.tenant_id, integ is not None, integ and integ.send_enabled, integ and integ.auto_send,
                 already_sent(lead))
        return "sent" if order and already_sent(lead) else "off"
    await fresh(db, integ)
    p = plan(order, integ)
    unmatched = [line["text"] for line in p["lines"] if line["product_id"] is None]
    if unmatched:  # an unlisted delivery place does not hold the order: it is sent, and staff call (below)
        why = ", ".join(f'"{t}" not on the menu' for t in unmatched)
        log.info("kitchen auto-send skipped for lead %s: needs staff (%s)", lead.id, why)
        await create_notification(
            db, event="kitchen_needs_staff", tenant_id=lead.tenant_id, source_table="sales_leads", entity_id=lead.id,
            subject=f"Order from {order['name']} needs you before it can go to the kitchen",
            message=f"{why}. Open Orders, pick it from the list and send it.", payload={"lead_id": str(lead.id)},
        )
        return "needs_staff"
    try:
        picks = [(line["product_id"], line["quantity"]) for line in p["lines"]]
        lines, loc = resolve(integ, picks, p["location"] or order["address"], allow_unlisted=True)
        await send(db, integ, lead, build_body(order, lines, loc, integ.email))
        if loc.get("unlisted"):
            await create_notification(
                db, event="kitchen_needs_staff", tenant_id=lead.tenant_id, source_table="sales_leads", entity_id=lead.id,
                subject=f"Call {order['name']}: delivery place not in our list",
                message=f"The order was sent with delivery charge Rs 0 to \"{loc['address']}\". Call {order['phone']} "
                "to confirm the place and the delivery charge.", payload={"lead_id": str(lead.id)},
            )
        return "sent"
    except (httpx.HTTPError, ValueError) as exc:
        detail = exc.response.text[:300] if isinstance(exc, httpx.HTTPStatusError) else str(exc)[:300]
        log.warning("kitchen auto-send failed for lead %s: %s", lead.id, detail)
        lead.meta = {**lead.meta, "kitchen_error": detail}
        await create_notification(
            db, event="kitchen_send_failed", tenant_id=lead.tenant_id, source_table="sales_leads", entity_id=lead.id,
            subject=f"Order from {order['name']} did not reach the kitchen",
            message="Open Orders and send it again, or call the cafe.", payload={"lead_id": str(lead.id)},
        )
        return "needs_staff"
