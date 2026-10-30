# fixtures/kei — CI test infrastructure for agentware-kei integration

| File | Purpose |
|---|---|
| `fake-kei-proxy-socket.py` | Unix-socket daemon that fakes the kei-proxy runtime socket protocol (authorize / tools:register / session). Used by existing CI (`go-test.yml`). |
| `fake-control-plane.py` | HTTP stub that fakes the kei-proxy control-plane API (WhoAmI / policy bundle / heartbeat / tool manifest / audit flush). Intended for `real-proxy-integration.yml` CI. Set `FAKE_CONTROL_PLANE_AUDIT_LOG` to keep the decision records the collector flushes. |
| `assert-audit-records.py` | Asserts that given tool decisions are in audit JSONL files (the proxy's spool, the flushed records) with a non-empty `invoking_subject`. Used by the `governed-call-audit` job. |
| `minimal-harness-tools.v3.json` | The `echo` / `delete_everything` tool manifest the real-proxy jobs register. |
| `test_fake_control_plane.py` | Pytest suite for `fake-control-plane.py`. Run with `pytest fixtures/kei/test_fake_control_plane.py -v`. |
| `policy.v1.json` | Fixture policy document for the `KeiProxyEvaluator` parity table. |
| `KEI_PROXY_SHA256` | Pinned SHA-256 checksums for the `kei-proxy_<version>_Linux_x86_64.tar.gz` archives CI downloads (`KEI_PROXY_VERSION`, and `AUDIT_PROXY_VERSION` in `real-proxy-integration.yml`). |

**Policy bundle shape** (served by `fake-control-plane.py`):

- Schema: `kei.policy-bundle/v2`
- Preconfigured rules: `echo` → allow, `delete_everything` → deny
- ETag is `sha256` of the canonical JSON body (used for `If-None-Match` / 304 flow)
