from main import app, VISION_PROMPT
assert app is not None
assert "physical verification signs" in VISION_PROMPT
assert "possible_sign" in VISION_PROMPT
assert "website UI text" in VISION_PROMPT
print("smoke tests passed")
