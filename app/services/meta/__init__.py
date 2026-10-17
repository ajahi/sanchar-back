"""Messaging channels by social_accounts.platform. Each module has send_text; Instagram and
Facebook also have send_image, fetch_customer_profile and fetch_recent_conversations."""
from app.services.meta import facebook, instagram, whatsapp

CHANNELS = {"instagram": instagram, "whatsapp": whatsapp, "facebook": facebook}
