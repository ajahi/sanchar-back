"""Photo tagging: a vision model looks at one shop photo and suggests a product name and a few tags.

Called once per photo from the tagging modal (admin-triggered), never per chat message. Same Groq key and
endpoint as the chat model; the model is VISION_MODEL so a retired one is a .env change.
"""
import json
import re

from app.core.config import settings
from app.schemas.shop_media import clean_tags
from app.services import http_client

PROMPT = """You label product photos for an online shop's catalogue. Look at the photo and answer with JSON only, no other text:
{{"title": "product name, 2-5 words, e.g. Red Matte Lipstick", "tags": ["3 to 5 short lowercase tags"]}}
Tags: the product type, its main colour, its finish or material, and the brand only if you can read it on the product. English words only.
{vocabulary}"""


def parse(content: str) -> dict:
    """{title, tags} from the model's answer: tolerates a <think> block or text around the JSON.
    Raises ValueError when there is no usable JSON object."""
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.S)
    found = re.search(r"\{.*\}", content, re.S)
    if not found:
        raise ValueError("no JSON in the vision answer")
    data = json.loads(found.group(0))
    if not isinstance(data, dict):
        raise ValueError("vision answer is not an object")
    title = data.get("title")
    tags = data.get("tags")
    return {
        "title": (" ".join(title.split())[:80] or None) if isinstance(title, str) else None,
        "tags": clean_tags(tags) if isinstance(tags, list) else [],
    }


async def suggest(image_url: str, vocabulary: list[str]) -> dict:
    """The model fetches the photo by its URL (our public storage, or Instagram's CDN). vocabulary = tags the
    shop already uses, so suggestions reuse them ("red", not "crimson" on one photo and "red" on the next)."""
    hint = f"Prefer these tags the shop already uses when they fit: {', '.join(vocabulary)}" if vocabulary else ""
    body = await http_client.request_json(
        "POST",
        settings.groq_url,
        headers={"Authorization": f"Bearer {settings.groq_api_key}"},
        json={
            "model": settings.vision_model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": PROMPT.format(vocabulary=hint)},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }
            ],
            "temperature": 0.2,
            "max_completion_tokens": 1500,  # room for a reasoning model's thinking before the JSON
        },
        timeout=45,
    )
    return parse(body["choices"][0]["message"]["content"] or "")
