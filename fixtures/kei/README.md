# fixtures/kei — CI test infrastructure for agentware-kei integration

| File | Purpose |
|---|---|
| `fake-kei-proxy-socket.py` | Unix-socket daemon that fakes the kei-proxy runtime socket protocol (authorize / tools:register / session). Used by existing CI (`go-test.yml`). |
| `fake-control-plane.py` | HTTP stub that fakes the kei-proxy control-plane API (WhoAmI / policy bundle / heartbeat). Intended for `real-proxy-integration.yml` CI. |
| `test_fake_control_plane.py` | Pytest suite for `fake-control-plane.py`. Run with `pytest fixtures/kei/test_fake_control_plane.py -v`. |
| `policy.v1.json` | Fixture policy document for the `KeiProxyEvaluator` parity table. |
| `KEI_PROXY_SHA256` | Pinned SHA-256 checksum for `kei-proxy_0.3.0_Linux_x86_64.tar.gz` — CI downloads and verifies against this. |

**Policy bundle shape** (served by `fake-control-plane.py`):

- Schema: `kei.policy-bundle/v2`
- Preconfigured rules: `echo` → allow, `delete_everything` → deny
- ETag is `sha256` of the canonical JSON body (used for `If-None-Match` / 304 flow)
