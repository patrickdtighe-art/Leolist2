
import asyncio
import os
from main import app, normalize_url, likely_listing_link

assert normalize_url("example.com") == "https://example.com"
assert normalize_url("https://example.com") == "https://example.com"
assert likely_listing_link("https://example.com/post/123", "https://example.com")
print("smoke tests passed")
