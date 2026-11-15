import json
import sys

context = json.load(sys.stdin)
print(json.dumps({"outcome": "completed", "payload": context.get("options")}))
