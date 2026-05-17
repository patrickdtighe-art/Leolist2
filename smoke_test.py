
from main import app, normalize_url, likely_detail_link, LEOLIST_CITIES, VISION_PROMPT

assert normalize_url("example.com") == "https://example.com"
assert "Northern Alberta / Grande Prairie" in LEOLIST_CITIES
assert "website UI text" in VISION_PROMPT
assert "physical verification signs" in VISION_PROMPT
assert likely_detail_link("https://example.com/listing/123", "https://example.com")
assert likely_detail_link("https://www.leolist.cc/personals/female-escorts/northern_alberta/grande_prairie", "https://www.leolist.cc")
print("smoke tests passed")
