"""Uses only the approved role credentials injected into this invocation."""
import json
import sys

import boto3


def main():
    options = json.load(sys.stdin)["options"]
    response = boto3.client("dynamodb").get_item(
        TableName=options["table"], Key={"orderId": {"S": options["orderId"]}}
    )
    status = response.get("Item", {}).get("status", {}).get("S")
    print(json.dumps({"outcome": "completed", "payload": {"orderId": options["orderId"], "status": status}}))


if __name__ == "__main__":
    main()
