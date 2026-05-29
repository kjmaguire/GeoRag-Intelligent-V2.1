#!/usr/bin/env bash
# =============================================================================
# scripts/emit_test_span.sh
#
# Emits a synthetic OTLP/HTTP span via curl and prints the trace_id on stdout.
# Used by phase0_step3_verify.sh to round-trip a span through the OTel
# collector → Tempo → Tempo query API.
#
# Why curl: bypasses the need for any SDK; works on any host with bash + curl
# + python3 (for hex generation). The OTLP/HTTP wire format accepts JSON,
# which is human-readable and easy to debug.
# =============================================================================

set -euo pipefail

OTEL_HTTP_ENDPOINT="${OTEL_HTTP_ENDPOINT:-http://localhost:4318}"

# 16-byte trace_id, 8-byte span_id, 32 / 16 hex chars respectively.
TRACE_ID="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
SPAN_ID="$(python3 -c 'import secrets; print(secrets.token_hex(8))')"

# OTLP timestamps are nanoseconds since unix epoch.
NOW_NS="$(python3 -c 'import time; print(int(time.time()*1e9))')"
END_NS="$((NOW_NS + 1000000))"  # +1ms

PAYLOAD=$(cat <<EOF
{
  "resourceSpans": [
    {
      "resource": {
        "attributes": [
          {"key": "service.name", "value": {"stringValue": "phase0-verify"}},
          {"key": "service.namespace", "value": {"stringValue": "georag"}},
          {"key": "deployment.environment", "value": {"stringValue": "dev"}}
        ]
      },
      "scopeSpans": [
        {
          "scope": {"name": "phase0_step3_verify"},
          "spans": [
            {
              "traceId": "${TRACE_ID}",
              "spanId":  "${SPAN_ID}",
              "name":    "phase0.step3.smoke",
              "kind":    1,
              "startTimeUnixNano": "${NOW_NS}",
              "endTimeUnixNano":   "${END_NS}",
              "status": {"code": 1},
              "attributes": [
                {"key": "phase", "value": {"stringValue": "0"}},
                {"key": "step",  "value": {"stringValue": "3"}}
              ]
            }
          ]
        }
      ]
    }
  ]
}
EOF
)

curl -sS -o /dev/null -w '' \
    -X POST "${OTEL_HTTP_ENDPOINT}/v1/traces" \
    -H 'Content-Type: application/json' \
    -d "${PAYLOAD}"

# Print just the trace_id so the verifier can capture it.
echo "${TRACE_ID}"
