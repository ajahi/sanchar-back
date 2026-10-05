"""One shared SSL context for every outbound httpx client.

httpx builds a fresh context (reloads the CA bundle, tens of ms of CPU) each time a client is created,
even for plain http:// URLs. With a client per call that blocked the event loop under load, so every
DB connection sat idle in its transaction and the pool ran dry. Pass `verify=SSL_CTX` instead.
"""
import httpx

SSL_CTX = httpx.create_ssl_context()
