//! Cross-language authorize-case replay for the kei-proxy CLI fallback client.
//!
//! Rust twin of `TestAuthorizeCases` in `go/kei/evaluator/evaluator_test.go`,
//! driven by the shared fixture `fixtures/kei/authorize-cases.v1.json` with
//! `fake-kei-proxy.sh` installed into a per-case temp directory. Every case
//! pins the exact argv the client must send, the decision the evaluator must
//! return, and the secrets that must never leak into reasons or argv.
#![cfg(unix)]

use std::collections::HashMap;
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde_json::Value;
use sha2::{Digest, Sha256};

use haikeilabs_agentware::evaluator::{
    Action, CallerContext, CliClient, Decision, KeiProxyEvaluator,
};

/// The shared fixtures live at the repository root, outside the crate.
fn fixture_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../fixtures/kei")
}

fn load_json(path: &Path) -> Value {
    let data =
        fs::read(path).unwrap_or_else(|err| panic!("read fixture {}: {err}", path.display()));
    serde_json::from_slice(&data)
        .unwrap_or_else(|err| panic!("decode fixture {}: {err}", path.display()))
}

/// Missing keys read as JSON null instead of panicking, like Go zero values.
fn field<'a>(value: &'a Value, key: &str) -> &'a Value {
    value.get(key).unwrap_or(&Value::Null)
}

fn text<'a>(value: &'a Value, key: &str) -> &'a str {
    value.get(key).and_then(Value::as_str).unwrap_or_default()
}

fn bool_field(value: &Value, key: &str) -> bool {
    value.get(key).and_then(Value::as_bool).unwrap_or(false)
}

fn str_list(value: &Value) -> Vec<String> {
    value
        .as_array()
        .map(|items| {
            items
                .iter()
                .map(|item| item.as_str().unwrap_or_default().to_string())
                .collect()
        })
        .unwrap_or_default()
}

/// Optional object fields: absent and null both read as `None`, mirroring the
/// Go nil maps the fixture round-trips through.
fn optional(value: &Value, key: &str) -> Option<Value> {
    match value.get(key) {
        None | Some(Value::Null) => None,
        Some(value) => Some(value.clone()),
    }
}

/// The fixture spells decisions out; Decision carries the Action enum.
fn action_str(action: Action) -> &'static str {
    match action {
        Action::Allow => "allow",
        Action::Deny => "deny",
        Action::Filter => "filter",
    }
}

/// Mirrors buildCaller: the caller context the harness carries into the
/// evaluator, seeded from the policy fixture and overridden per case.
fn build_caller(table: &Value, policy: &Value, case: &Value) -> CallerContext {
    let user_key = text(field(case, "input"), "user");
    let subject = policy
        .get("users")
        .and_then(|users| users.get(user_key))
        .and_then(|user| user.get("id"))
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();

    let mut metadata = HashMap::new();
    metadata.insert(
        "span_id".to_string(),
        text(field(table, "caller_defaults"), "span_id").to_string(),
    );
    metadata.insert(
        "agent_version".to_string(),
        text(field(policy, "agent"), "version").to_string(),
    );
    metadata.insert(
        "framework".to_string(),
        text(field(policy, "agent"), "framework").to_string(),
    );
    metadata.insert(
        "workspace_id".to_string(),
        text(field(policy, "workspace"), "id").to_string(),
    );

    let mut caller = CallerContext {
        user_id: subject.clone(),
        invoking_subject: subject,
        metadata,
        ..Default::default()
    };

    if let Some(overrides) = field(case, "input").get("caller").filter(|v| !v.is_null()) {
        let omit_user = bool_field(overrides, "omit_user");
        let user_id = text(overrides, "user_id");
        if !user_id.is_empty() || omit_user {
            caller.user_id = user_id.to_string();
        }
        let invoking_subject = text(overrides, "invoking_subject");
        if !invoking_subject.is_empty() || omit_user {
            caller.invoking_subject = invoking_subject.to_string();
        }
        if bool_field(overrides, "omit_workspace") {
            caller.metadata.remove("workspace_id");
        }
        caller.parent_span = text(overrides, "parent_span").to_string();
        caller.delegation_depth = overrides
            .get("delegation_depth")
            .and_then(Value::as_i64)
            .unwrap_or(0) as _;
    }
    caller
}

/// Mirrors buildEnv: the child environment the CLI client is told to use —
/// PATH, deliberately leaked-looking stripped variables, and the runtime-token
/// canary unless the case omits it.
fn build_env(table: &Value, case: &Value) -> HashMap<String, String> {
    let mut env = HashMap::new();
    env.insert("PATH".to_string(), "/usr/bin:/bin".to_string());
    for name in str_list(field(field(table, "child_env"), "stripped")) {
        env.insert(name.clone(), format!("leaked-{name}"));
    }
    if !bool_field(field(case, "env"), "omit_token") {
        let token = text(field(table, "canaries"), "runtime_token").to_string();
        env.insert("KEI_RUNTIME_TOKEN".to_string(), token);
    }
    env
}

/// Mirrors installFake: installs the fake kei-proxy into `dir` — the POSIX sh
/// script plus the stdout/stderr/exit_code/hang response files it reads — and
/// returns the executable path the client must be pointed at. A
/// missing_binary case installs nothing so resolution fails.
fn install_fake(case: &Value, dir: &Path) -> PathBuf {
    let fake = dir.join("kei-proxy");
    if bool_field(field(case, "proxy"), "missing_binary") {
        return fake;
    }
    let script = fs::read(fixture_dir().join("fake-kei-proxy.sh")).expect("read fake proxy");
    let mut target = "kei-proxy".to_string();
    if bool_field(field(case, "proxy"), "symlink") {
        target = "kei-proxy-real".to_string();
        std::os::unix::fs::symlink(dir.join(&target), &fake).expect("symlink kei-proxy");
    }
    let exit_code = field(field(case, "proxy"), "exit_code")
        .as_i64()
        .unwrap_or(0)
        .to_string();
    let mut files: Vec<(&str, Vec<u8>)> = vec![
        (&target, script),
        (
            "stdout",
            text(field(case, "proxy"), "stdout").as_bytes().to_vec(),
        ),
        (
            "stderr",
            text(field(case, "proxy"), "stderr").as_bytes().to_vec(),
        ),
        ("exit_code", exit_code.into_bytes()),
    ];
    if bool_field(field(case, "proxy"), "hang") {
        files.push(("hang", Vec::new()));
    }
    for (name, body) in files {
        let path = dir.join(name);
        fs::write(&path, &body).unwrap_or_else(|err| panic!("write {}: {err}", path.display()));
        let mut permissions = fs::metadata(&path).expect("metadata").permissions();
        permissions.set_mode(0o755);
        fs::set_permissions(&path, permissions).expect("set permissions");
    }
    fake
}

/// Mirrors expectedSHA256: the case's pin — "installed" is the digest of the
/// fake's bytes.
fn expected_sha256(case: &Value) -> Option<String> {
    let pin = text(field(case, "client"), "expected_sha256");
    if pin != "installed" {
        return if pin.is_empty() {
            None
        } else {
            Some(pin.to_string())
        };
    }
    let script = fs::read(fixture_dir().join("fake-kei-proxy.sh")).expect("read fake proxy");
    let digest = Sha256::digest(&script);
    Some(digest.iter().map(|byte| format!("{byte:02x}")).collect())
}

/// Mirrors caseDir: a temp dir with symlinks resolved (macOS /var is a
/// symlink), so reason paths match what the client resolves.
fn case_dir() -> PathBuf {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock")
        .as_nanos();
    let dir = std::env::temp_dir().join(format!("aw-authorize-{}-{nanos}", std::process::id()));
    fs::create_dir_all(&dir).expect("create temp dir");
    fs::canonicalize(&dir).expect("resolve temp dir")
}

/// Mirrors runCase: one evaluator call through a CLI client pinned to the
/// fake, returning the decision and the case dir holding the fake's records.
fn run_case(table: &Value, policy: &Value, case: &Value) -> (Decision, PathBuf) {
    let dir = case_dir();
    let client = CliClient {
        executable: install_fake(case, &dir),
        expected_sha256: expected_sha256(case),
        timeout: Duration::from_millis(field(table, "timeout_ms").as_u64().unwrap_or(0)),
        env: Some(build_env(table, case)),
    };
    let evaluator = KeiProxyEvaluator::with_client(client);
    let caller = build_caller(table, policy, case);
    let decision = evaluator.evaluate(
        text(field(case, "input"), "tool"),
        field(field(case, "input"), "args"),
        &caller,
    );
    (decision, dir)
}

/// True once `pid` is gone: `kill -0` fails for a dead pid. The evaluator must
/// have killed the hung fake, so this settles quickly.
fn process_dead(pid: i32) -> bool {
    for _ in 0..50 {
        let alive = Command::new("sh")
            .arg("-c")
            .arg(format!("kill -0 {pid} 2>/dev/null"))
            .status()
            .map(|status| status.success())
            .unwrap_or(true);
        if !alive {
            return true;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    false
}

#[test]
fn authorize_cases() {
    let table = load_json(&fixture_dir().join("authorize-cases.v1.json"));
    let policy = load_json(&fixture_dir().join(text(&table, "policy")));
    let canaries: Vec<(String, String)> = field(&table, "canaries")
        .as_object()
        .map(|entries| {
            entries
                .iter()
                .map(|(name, value)| (name.clone(), value.as_str().unwrap_or_default().to_string()))
                .collect()
        })
        .unwrap_or_default();
    let forbidden_flags = str_list(field(field(&table, "argv_forbidden"), "flags"));
    let stripped = str_list(field(field(&table, "child_env"), "stripped"));

    let mut mismatches: Vec<String> = Vec::new();
    for case in field(&table, "cases").as_array().expect("fixture cases") {
        let id = text(case, "id").to_string();
        let want = field(case, "expected");
        let (decision, dir) = run_case(&table, &policy, case);
        let caller = build_caller(&table, &policy, case);

        // Caller identity: the delegation contract must survive the hop.
        if let Some(want_user) = optional(want, "caller_user_id") {
            let want_user = want_user.as_str().unwrap_or_default();
            if caller.user_id != want_user {
                mismatches.push(format!(
                    "case {id}: caller user_id {:?}, want {want_user:?}",
                    caller.user_id
                ));
            }
        }
        if bool_field(want, "workspace_absent") && caller.metadata.contains_key("workspace_id") {
            mismatches.push(format!(
                "case {id}: workspace_id unexpectedly present in caller metadata"
            ));
        }
        for key in str_list(field(want, "context_absent")) {
            if caller.metadata.contains_key(&key) {
                mismatches.push(format!("case {id}: context key {key} unexpectedly present"));
            }
        }

        // Decision.
        let action = action_str(decision.action);
        if action != text(want, "action") {
            mismatches.push(format!(
                "case {id}: action {action:?}, want {:?}",
                text(want, "action")
            ));
        }
        let class = format!("kei-proxy {}", text(want, "reason_class"));
        let reason_ok =
            decision.reason == class || decision.reason.starts_with(&format!("{class}:"));
        if !reason_ok {
            mismatches.push(format!(
                "case {id}: reason {:?}, want class {:?}",
                decision.reason,
                text(want, "reason_class")
            ));
        }
        if decision.rule != text(want, "rule") {
            mismatches.push(format!(
                "case {id}: rule {:?}, want {:?}",
                decision.rule,
                text(want, "rule")
            ));
        }
        if decision.enrollment != optional(want, "enrollment") {
            mismatches.push(format!(
                "case {id}: enrollment {:?}, want {:?}",
                decision.enrollment,
                optional(want, "enrollment")
            ));
        }
        if decision.connect != optional(want, "connect") {
            mismatches.push(format!(
                "case {id}: connect {:?}, want {:?}",
                decision.connect,
                optional(want, "connect")
            ));
        }
        if decision.reason_code.as_deref().unwrap_or("") != text(want, "reason_code") {
            mismatches.push(format!(
                "case {id}: reason_code {:?}, want {:?}",
                decision.reason_code,
                text(want, "reason_code")
            ));
        }
        if decision.hint.as_deref().unwrap_or("") != text(want, "hint") {
            mismatches.push(format!(
                "case {id}: hint {:?}, want {:?}",
                decision.hint,
                text(want, "hint")
            ));
        }
        let reason_contains = text(want, "reason_contains");
        if !reason_contains.is_empty() && !decision.reason.contains(reason_contains) {
            mismatches.push(format!(
                "case {id}: reason {:?} does not contain {reason_contains:?}",
                decision.reason
            ));
        }
        if bool_field(want, "reason_contains_binary_path") {
            let binary = dir.join("kei-proxy").display().to_string();
            if !decision.reason.contains(&binary) {
                mismatches.push(format!(
                    "case {id}: reason {:?} does not include the attempted binary path {binary:?}",
                    decision.reason
                ));
            }
        }

        // Canaries: no secret may reach the reason. (The Go twin also checks
        // the captured logs; the Rust evaluator has no logger surface yet.)
        for (name, canary) in &canaries {
            if decision.reason.contains(canary.as_str()) {
                mismatches.push(format!("case {id}: canary {name} leaked into the reason"));
            }
        }

        // The fake records the argv it received in dir/argv.
        let argv_path = dir.join("argv");
        let invoked = argv_path.exists();
        if invoked != bool_field(want, "invoked") {
            mismatches.push(format!(
                "case {id}: invoked {invoked}, want {}",
                bool_field(want, "invoked")
            ));
        }
        if !invoked {
            continue;
        }
        let argv_data = fs::read_to_string(&argv_path).expect("read argv");
        let argv: Vec<String> = argv_data
            .trim_end_matches('\n')
            .split('\n')
            .map(str::to_string)
            .collect();
        let token = text(field(&table, "canaries"), "runtime_token");
        for arg in &argv {
            if !token.is_empty() && arg.contains(token) {
                mismatches.push(format!("case {id}: runtime token appeared in argv"));
            }
        }
        for flag in &forbidden_flags {
            for arg in &argv {
                if arg == flag {
                    mismatches.push(format!("case {id}: {flag} must not be sent"));
                }
            }
        }
        if let Some(want_argv) = optional(want, "argv") {
            if argv != str_list(&want_argv) {
                mismatches.push(format!(
                    "case {id}: argv {argv:?}, want {:?}",
                    str_list(&want_argv)
                ));
            }
        }

        // Child environment: stripped variables must never reach the fake.
        if let Ok(child_env) = fs::read_to_string(dir.join("env")) {
            for name in &stripped {
                let leaked = format!("leaked-{name}");
                if child_env.contains(&leaked) {
                    mismatches.push(format!(
                        "case {id}: stripped env var {name} leaked to the child"
                    ));
                }
            }
            if bool_field(field(case, "env"), "omit_token")
                && !token.is_empty()
                && child_env.contains(token)
            {
                mismatches.push(format!(
                    "case {id}: runtime token reached the child despite omit_token"
                ));
            }
        }

        // A hung fake must have been killed, process group and all.
        if bool_field(want, "group_killed") {
            let hang_pid = fs::read_to_string(dir.join("hang_pid")).expect("read hang_pid");
            let pids: Vec<i32> = hang_pid
                .split_whitespace()
                .filter_map(|word| word.parse().ok())
                .collect();
            if pids.is_empty() {
                mismatches.push(format!("case {id}: hang_pid contains no pid"));
            }
            for pid in pids {
                if !process_dead(pid) {
                    mismatches.push(format!("case {id}: hang process {pid} still alive"));
                }
            }
        }
    }
    assert!(
        mismatches.is_empty(),
        "authorize_cases mismatches ({}):\n{}",
        mismatches.len(),
        mismatches.join("\n")
    );
}
