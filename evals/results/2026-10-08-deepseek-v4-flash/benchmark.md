# Eval benchmark: deepseek-v4-flash

- Model: `deepseek-ai/DeepSeek-V4-Flash`
- Harness: agentware
- Created: 2026-10-08T09:00:09+00:00
- Git SHA: `a27976371f3927ee445f64a2f2197f101f69385c`
- Threshold: 95%

| Suite | Kind | Passed | Failed | Errors | Total | Pass rate | Mean case time | Status |
|---|---|---:|---:|---:|---:|---:|---:|---|
| agentware.beta-tools | agent | 23 | 1 | 0 | 24 | 95.8% | 32.8s | PASS |

## Failing cases: agentware.beta-tools

| Case | Reason |
|---|---|
| beta_invalid_issue_number_non_numeric | expected tool 'github.get_issue', no tool was called |
