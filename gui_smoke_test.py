
from fastapi.testclient import TestClient
import main

client = TestClient(main.app)

r = client.get("/")
assert r.status_code == 200
assert "Verification Sign Scanner Pro" in r.text
assert "Start full scanner" in r.text

r = client.get("/health")
assert r.status_code == 200

print("gui smoke tests passed")
