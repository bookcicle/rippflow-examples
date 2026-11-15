"""Run with python3 -m unittest discover -s examples/async-callback -p 'test_*.py'."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch


class CallbackAcceptanceTests(unittest.TestCase):
    def run_adapter(self, payload, status=200, function_error=False):
        calls = []

        class Client:
            def invoke(self, **kwargs):
                calls.append(kwargs)
                return {"StatusCode": status, "FunctionError": "Unhandled" if function_error else None,
                        "Payload": io.BytesIO(json.dumps(payload).encode())}

        spec = importlib.util.spec_from_file_location("example_adapter", Path(__file__).with_name("adapter.py"))
        module = importlib.util.module_from_spec(spec)
        event = {"invocationId": "stable-invocation", "idempotencyKey": "stable-key", "input": {"question": "test"},
                 "callback": {"functionName": "dedicated-ingress", "envelope": {"action": "async_callback", "callbackToken": "scoped-token"}}}
        original = copy.deepcopy(event)
        with patch.dict(sys.modules, {"boto3": types.SimpleNamespace(client=lambda *args: Client())}):
            spec.loader.exec_module(module)
            result = module.lambda_handler(event, None)
        self.assertEqual(event, original)
        self.assertEqual(calls[0]["FunctionName"], "dedicated-ingress")
        self.assertEqual(calls[0]["InvocationType"], "RequestResponse")
        forwarded = json.loads(calls[0]["Payload"])
        self.assertEqual(forwarded["status"], "SUCCEEDED")
        self.assertEqual(forwarded["callbackToken"], "scoped-token")
        return result

    def test_only_confirmed_callback_acceptance_succeeds(self):
        self.assertEqual(self.run_adapter({"status": "async_callback_processed"}), {"status": "callback_accepted"})

    def test_transport_success_never_masks_application_rejection(self):
        for payload, kwargs in (({"status": "controls_canceled"}, {}), ({}, {}), ([], {}),
                                ({"status": "async_callback_processed"}, {"status": 202}),
                                ({"status": "async_callback_processed"}, {"function_error": True})):
            with self.subTest(payload=payload, kwargs=kwargs), self.assertRaises(RuntimeError):
                self.run_adapter(payload, **kwargs)


if __name__ == "__main__":
    unittest.main()
