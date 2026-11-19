"""Behaviour rules: what a funnel's .md file says the bot must or must not do, enforced by code.

A funnel keeps its rules in its checks, as {"type": "rule", ...}; in its .md file they are the `rules:` list
(services/funnel_md.py), so a behaviour change is a readable edit with a version and a diff. Code holds the
building blocks, written once; a rule picks them and gives them its words:

  name                      what the rule is for, in plain words (shown in errors)
  when                      what is detected in the customer's messages: one of WHEN
  tell_bot                  the note for the model while it holds ({fulfilment}, {quote} are filled in);
                            without it, the default note for its `when`
  remove_from_reply         questions taken out of a reply that asks them anyway: REMOVABLE
  replace_reply_if_it_asks  shop_closed only: a reply asking these becomes the closed message: REPLACEABLE
  delivery_words, pickup_words      customer_said_fulfilment / customer_gave_place; a word matches what starts
                                    with it ("lyaidi" covers lyaidinu, lyaidinus); without them, the lists below
  describing_words, chat_words      menu_item_named: words never taken for a dish name
  examples                  [{customer: "..." or [...], expect: {fulfilment: delivery | pickup | none}} or
                            {place: true | false}]: checked when the funnel is saved; a wrong one blocks the save

A new rule made of these is an edit to the .md file. A new kind of detection is one function here, once.
"""
import re
from dataclasses import dataclass, field

from app.services import kitchen

WHEN = ("customer_said_fulfilment", "customer_gave_place", "menu_item_named", "shop_closed")
REMOVABLE = ("fulfilment_question", "address_question")
REPLACEABLE = ("order_details",)
_LISTS = ("delivery_words", "pickup_words", "describing_words", "chat_words")
_KEYS = {"type", "name", "when", "tell_bot", "remove_from_reply", "replace_reply_if_it_asks", "examples", *_LISTS}

DELIVERY_WORDS = ["deliver", "bring", "lyaidi", "lyaide", "lyaun", "pathaidi", "pathaide", "pathaun", "ghar samma",
                  "ghar ma", "ल्याइदि", "पठाइदि"]
PICKUP_WORDS = ["pick up", "take away", "takeaway", "lina aau", "lina au", "aaera lan", "aayera lan", "ayera lan",
                "aera lan", "aafai lin", "aafai aau", "parcel lin"]
TELL_BOT = {
    "customer_said_fulfilment": 'They already said they want {fulfilment} ("{quote}"). Never ask delivery or pick up.',
    "customer_gave_place": ('They already gave their place ("{quote}"): that is their address, never ask for it '
                            "again. Ask only for what is still missing (the delivery area only if their place is not "
                            "one of our areas)."),
}
NOTE = "Note from the business, not the customer: "

DEFAULT_RULES = [  # funnels.DEFAULT, the built-in order steps: any business, so nothing about delivery
    {"type": "rule", "name": "No order questions while the shop is closed", "when": "shop_closed",
     "replace_reply_if_it_asks": ["order_details"]},
    {"type": "rule", "name": "Name every menu item the customer's word points to", "when": "menu_item_named"},
]


@dataclass
class Context:
    texts: list[str]  # the customer's messages, oldest first, the one being answered last
    areas: list[dict] = field(default_factory=list)  # delivery areas (kitchen)
    catalog: list[dict] = field(default_factory=list)  # the synced menu (kitchen)
    spelled: dict[str, str] = field(default_factory=dict)  # the owner's {spellings}
    closed: str | None = None  # the closed message, while closed


@dataclass
class Outcome:
    notes: list[str] = field(default_factory=list)
    remove: set[str] = field(default_factory=set)
    replace_if_asks: set[str] = field(default_factory=set)


def of(funnel) -> list[dict]:
    return [c for c in (getattr(funnel, "checks", None) or []) if isinstance(c, dict) and c.get("type") == "rule"]


def _words(words: list[str]) -> re.Pattern:
    """Any of these words or phrases, each matching what starts with it; spaces and hyphens are optional."""
    parts = []
    for w in (x.strip().lower() for x in words if isinstance(x, str)):
        if w:
            body = r"[\s-]*".join(re.escape(x) for x in re.split(r"[\s-]+", w))
            parts.append((r"\b" if w[0].isascii() else "") + body)
    return re.compile("|".join(parts) or r"(?!)", re.IGNORECASE)


def _named_area(text: str, areas: list[dict]) -> bool:
    loc = kitchen.match_location(text, areas) if areas else None
    return loc is not None and loc["address"] != kitchen.PICKUP


def fulfilment(rule: dict, ctx: Context) -> tuple[str, str] | None:
    """("delivery" | "pickup", the message that said it); the latest message that says one wins."""
    pickup, delivery = _words(rule.get("pickup_words") or PICKUP_WORDS), _words(rule.get("delivery_words") or DELIVERY_WORDS)
    found = None
    for text in ctx.texts:
        if pickup.search(text):
            found = ("pickup", text)
        elif delivery.search(text) or _named_area(text, ctx.areas):
            found = ("delivery", text)
    return found


# "<place> ma" (Nepali "at <place>") or "to my office" / "to Baneshwor", in a message that asks for delivery.
# ponytail: a word before "ma" that is not one of these counts as a place ("Expert ma", "office ma", "store ma").
_LOCATIVE = re.compile(r"\b([a-z0-9][\w'-]*)\s+ma\b", re.IGNORECASE)
_TO_PLACE = re.compile(r"\b(?:[Tt]o|[Aa]t)\s+(?:(?:my|our|the)\s+\w+|[A-Z]\w+)")
_NOT_PLACE = set("ma malai mero hamro yo tyo kati ahile aaja bholi pachi aba ek euta dui duita tin ota wata plate "
                 "ghar order khana".split())


def place(rule: dict, ctx: Context) -> str | None:
    """The message in which the customer named where to deliver (one of our areas, or a place with a delivery
    word), or None. Pick up afterwards cancels it."""
    pickup, delivery = _words(rule.get("pickup_words") or PICKUP_WORDS), _words(rule.get("delivery_words") or DELIVERY_WORDS)
    found = None
    for text in ctx.texts:
        if pickup.search(text):
            found = None
        elif _named_area(text, ctx.areas) or (delivery.search(text) and (
                any(m.group(1).lower() not in _NOT_PLACE and not m.group(1).isdigit() for m in _LOCATIVE.finditer(text))
                or _TO_PLACE.search(text))):
            found = text
    return found


def _lower(rule: dict, key: str) -> set[str] | None:
    return {w.strip().lower() for w in rule[key] if isinstance(w, str)} if rule.get(key) is not None else None


def _note(rule: dict, when: str, **values: str) -> str:
    text = rule.get("tell_bot") or TELL_BOT[when]
    for k, v in values.items():
        text = text.replace("{" + k + "}", v)
    return NOTE + text


def _quote(text: str) -> str:
    return text.strip().replace('"', "'")[:120]


def run(funnel, ctx: Context) -> Outcome:
    """What the funnel's rules add to this reply: notes for the model, and what to fix in its reply (finish)."""
    out = Outcome()
    for rule in of(funnel):
        when, remove = rule.get("when"), set(rule.get("remove_from_reply") or [])
        if when == "customer_said_fulfilment" and (said := fulfilment(rule, ctx)):
            out.notes.append(_note(rule, when, fulfilment="DELIVERY" if said[0] == "delivery" else "PICK UP",
                                   quote=_quote(said[1])))
            out.remove |= remove
        elif when == "customer_gave_place" and (where := place(rule, ctx)):
            out.notes.append(_note(rule, when, quote=_quote(where)))
            out.remove |= remove
        elif when == "menu_item_named" and ctx.texts:
            note = kitchen.item_hint(ctx.texts[-1], ctx.catalog, ctx.spelled, _lower(rule, "describing_words"),
                                     _lower(rule, "chat_words"))
            if note:
                out.notes.append(note + (" " + rule["tell_bot"] if rule.get("tell_bot") else ""))
        elif when == "shop_closed" and ctx.closed:
            out.replace_if_asks |= set(rule.get("replace_reply_if_it_asks") or [])
            if rule.get("tell_bot"):
                out.notes.append(_note(rule, when))
    return out


# ---- fixing a reply that asks what the customer already answered ----

# A sentence asking the customer for an order detail: one of these words in a question, or with a "give / send /
# tell" verb. ponytail: English and Romanized Nepali; Devanagari replies are not caught.
_ORDER_WORD = re.compile(
    r"delivery\s*(ki|or|wa|va)\s*pick|pick\s*-?\s*up|address|thegana|phone|number|\bnaam|\bname\b|kun area|which area"
    r"|kati\s*(ota|wata|plate|piece)|how many",
    re.IGNORECASE,
)
_ASK_VERB = re.compile(r"dinus|dinuhos|pathaunus|pathaidinus|bhannus|share|tell me|let me know|send", re.IGNORECASE)


def _sentences(reply: str) -> list[str]:
    return re.split(r"(?<=[.!?\n])", reply)


def asks_order_details(reply: str) -> bool:
    """True when the reply asks the customer for something only an order needs."""
    return any(_ORDER_WORD.search(s) and ("?" in s or _ASK_VERB.search(s)) for s in _sentences(reply))


_CHOICE = re.compile(
    r"[^.!?\n]*\b(delivery\b[^.!?\n]*\bpick\s*-?\s*up|pick\s*-?\s*up\b[^.!?\n]*\bdelivery)\b[^.!?\n]*\?[ \t]*",
    re.IGNORECASE,
)
_IF_PICKUP = re.compile(r"[ \t]*\b(if\s+(you\s+want\s+)?pick\s*-?\s*up|pick\s*-?\s*up\s+ho\s+bhane)\b[^.!?\n]*[.!?]?",
                        re.IGNORECASE)
_IF_DELIVERY = re.compile(r"\b(if\s+(it'?s\s+)?delivery|delivery\s+ho\s+bhane)\s*,?\s*", re.IGNORECASE)


def _tidy(out: str, reply: str) -> str:
    if out == reply:
        return reply
    out = re.sub(r"[ \t]+\n", "\n", re.sub(r"[ \t]{2,}", " ", out))
    out = re.sub(r"(^|[.!?][ \t]+|\n)([a-z])", lambda m: m.group(1) + m.group(2).upper(), out)  # sentence starts
    return re.sub(r"\n{3,}", "\n\n", out).strip() or reply


def drop_fulfilment_question(reply: str) -> str:
    """Without "Delivery ki pickup?" and its "If pickup, ..."; "If delivery, please tell ..." keeps its ask."""
    return _tidy(_IF_DELIVERY.sub("", _IF_PICKUP.sub(" ", _CHOICE.sub(" ", reply))), reply)


_ADDRESS = re.compile(r"\b(address|thegana|location|kaha\s+(cha|ho|ma|parcha)|where)\b", re.IGNORECASE)
_OTHER_ASK = re.compile(r"\b(phone|number|naam|name|area|which|kun|item|quantity|kati)\b", re.IGNORECASE)
_ADDRESS_PART = re.compile(r"\b(your\s+)?(address|thegana)\s*(,|\bra\b|\band\b|&)\s*|\s*(,|\bra\b|\band\b|&)\s*"
                           r"(your\s+)?(address|thegana)\b", re.IGNORECASE)


def drop_address_question(reply: str) -> str:
    """Without a sentence that only asks for the address; one that also asks the phone loses the address part."""
    kept = []
    for s in _sentences(reply):
        if _ADDRESS.search(s) and ("?" in s or _ASK_VERB.search(s)):
            if not _OTHER_ASK.search(s):
                continue
            s = _ADDRESS_PART.sub(" ", s)
        kept.append(s)
    return _tidy("".join(kept), reply)


def finish(reply: str, out: Outcome, closed: str | None) -> str:
    """The model's reply with what the rules fix: the closed message instead of order questions, and the
    questions the customer already answered taken out."""
    if closed and "order_details" in out.replace_if_asks and asks_order_details(reply):
        return closed
    if "fulfilment_question" in out.remove:
        reply = drop_fulfilment_question(reply)
    if "address_question" in out.remove:
        reply = drop_address_question(reply)
    return reply


# ---- checking a rule when a funnel is saved ----

def problems(rule: dict) -> list[str]:
    """What is wrong with a rule, in words for the super admin ([] = fine), its examples run included."""
    name = rule.get("name") or rule.get("when") or "A rule"
    when, errs = rule.get("when"), []
    if when not in WHEN:
        return [f'{name}: "when" must be one of {", ".join(WHEN)}.']
    if unknown := set(rule) - _KEYS:
        errs.append(f"{name}: unknown setting(s) {', '.join(sorted(unknown))}.")
    if bad := set(rule.get("remove_from_reply") or []) - set(REMOVABLE):
        errs.append(f"{name}: remove_from_reply can only hold {', '.join(REMOVABLE)} (not {', '.join(sorted(bad))}).")
    if rule.get("replace_reply_if_it_asks"):
        if when != "shop_closed":
            errs.append(f"{name}: replace_reply_if_it_asks only works with when: shop_closed.")
        elif bad := set(rule["replace_reply_if_it_asks"]) - set(REPLACEABLE):
            errs.append(f"{name}: replace_reply_if_it_asks can only hold {', '.join(REPLACEABLE)}.")
    for key in _LISTS:
        if key in rule and not (isinstance(rule[key], list) and all(isinstance(w, str) for w in rule[key])):
            errs.append(f"{name}: {key} must be a list of words.")
    if "tell_bot" in rule and not isinstance(rule["tell_bot"], str):
        errs.append(f"{name}: tell_bot must be text.")
    if errs:
        return errs
    for ex in rule.get("examples") or []:
        texts = ex.get("customer") if isinstance(ex, dict) else None
        texts = [texts] if isinstance(texts, str) else texts
        expect = ex.get("expect") if isinstance(ex, dict) else None
        if not texts or not isinstance(expect, dict):
            errs.append(f"{name}: an example needs customer and expect.")
            continue
        ctx, shown = Context(texts=list(texts)), " / ".join(texts)
        if when == "customer_said_fulfilment" and "fulfilment" in expect:
            got = (fulfilment(rule, ctx) or ("none",))[0]
            if got != str(expect["fulfilment"]).lower():
                errs.append(f'{name}: "{shown}" should be {expect["fulfilment"]}, the words give {got}.')
        elif when == "customer_gave_place" and "place" in expect:
            got = place(rule, ctx) is not None
            if got != bool(expect["place"]):
                errs.append(f'{name}: "{shown}" should {"" if expect["place"] else "not "}count as a place.')
        else:
            errs.append(f"{name}: examples for when: {when} can't be checked (use fulfilment or place).")
    return errs
