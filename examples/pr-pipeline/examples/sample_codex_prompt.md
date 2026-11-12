# Add Structured Request-Timeout Diagnostics & Retry-After Parsing

## Goal
Implement structured HTTP request-timeout diagnostics and RFC 9110 `Retry-After` header parsing across the client and CLI layers.

## Acceptance Criteria
1. Parse both integer delay-seconds (`Retry-After: 30`) and HTTP-date (`Retry-After: Wed, 21 Oct 2026 07:28:00 GMT`) headers cleanly.
2. Surface a structured `TimeoutDiagnostics` payload (`connectMs`, `tlsHandshakeMs`, `firstByteMs`, `retryAfterMs`) whenever a request times out or returns `429` / `503`.
3. Add deterministic unit tests covering integer delays, HTTP-date delays, malformed header values, and timeout error formatting.
