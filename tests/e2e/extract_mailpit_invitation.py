"""Emit exactly one synthetic invitation URL to a caller-owned private file."""

import json
import re
from urllib.request import urlopen

with urlopen("http://mail:8025/api/v1/messages", timeout=6) as response:
    mailbox = json.load(response)

ids = [
    message["ID"]
    for message in mailbox["messages"]
    if any(
        target["Address"] == "first-owner@example.invalid" for target in message["To"]
    )
]
if len(ids) != 1:
    raise SystemExit("Exactly one synthetic first-Owner mail is required")

with urlopen(f"http://mail:8025/api/v1/message/{ids[0]}", timeout=6) as response:
    delivered = json.load(response)

if delivered["Subject"] != "[SYNTHETIC ONLY] CarePulse invitation":
    raise SystemExit("Unexpected synthetic invitation kind")
match = re.search(r"https?://[^\s]+/auth/invite-accept#[^\s]+", delivered["Text"])
if not match:
    raise SystemExit("Committed invitation link unavailable")
print(match.group())
