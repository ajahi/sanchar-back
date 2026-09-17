"""Normalising Meta identifiers so the webhook path and the Graph path agree.

Meta hands back the same message under two different shapes:

* Webhooks call it `message.mid` (e.g. `mid.$cAAC3Jml0RcBi3FZGm1cqxD-bVYg1`).
* The Conversations API returns a Graph Message node whose `id` historically carries an
  extra `m_` prefix for the identical message (e.g. `m_mid.$cAAC3Jml0RcBi3FZGm1cqxD-bVYg1`).

Both land in `messages.external_message_id`, which has a unique index, so unless the two
forms are collapsed to one the same message is stored twice — once by the backfill and
again when the webhook echo arrives. `normalize_message_id` is applied on both sides.

The rule is deliberately narrow. Newer Instagram ids already begin with `m` (e.g.
`aWdfZAG1f...`) and must not be mangled, so a leading `m_` is stripped **only** when what
follows is a recognisable `mid.` identifier.
"""
from __future__ import annotations

from typing import Optional

# Prefix Meta adds on the Graph response but not in the webhook payload.
_GRAPH_PREFIX = "m_"
# Prefix that identifies a webhook-style message id.
_MID_PREFIX = "mid."


def normalize_message_id(raw: Optional[str]) -> Optional[str]:
    """Collapse the Graph and webhook spellings of a message id to a single form."""
    if not raw:
        return None
    value = raw.strip()
    if not value:
        return None

    stripped = value
    # `m_mid.x` -> `mid.x`. Peeling repeatedly also handles a doubled prefix.
    while stripped.startswith(_GRAPH_PREFIX) and stripped[
        len(_GRAPH_PREFIX) :
    ].startswith(_MID_PREFIX):
        stripped = stripped[len(_GRAPH_PREFIX) :]

    return stripped
