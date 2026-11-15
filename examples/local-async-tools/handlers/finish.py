#!/usr/bin/env python3
import json
import sys

json.load(sys.stdin)
print(json.dumps({"outcome": "completed", "payload": {"callbacksJoined": True}}))
