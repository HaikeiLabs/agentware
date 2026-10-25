use agentware_kei::fixture::load_contract;

#[test]
fn loads_runtime_socket_constants_contract() {
    let contract = load_contract("runtime-socket/constants.json")
        .expect("runtime socket constants contract should load");

    assert_eq!(
        contract["socket"]["default_path"],
        "/run/kei-proxy/runtime.sock"
    );
}

use agentware_kei::runtimelink as rl;
use serde_json::{Value, json};
use std::collections::BTreeMap;

fn obj(value: &Value) -> BTreeMap<String, String> {
    value
        .as_object()
        .unwrap()
        .iter()
        .map(|(k, v)| (k.clone(), v.as_str().unwrap().to_owned()))
        .collect()
}
fn config_value(c: &rl::Config) -> Value {
    json!({"enabled":c.enabled,"token_env":c.token_env,"control_plane_url":c.control_plane_url,"binary_path":c.binary_path,"binary_sha256":c.binary_sha256,"interval_s":c.interval_s,"beat_timeout_s":c.beat_timeout_s,"grace_s":c.grace_s,"restart_min_s":c.restart_min_s,"restart_max_s":c.restart_max_s,"stable_reset_s":c.stable_reset_s,"stop_timeout_s":c.stop_timeout_s,"log_count":c.log_count,"harness":{"kind":c.harness.kind,"version":c.harness.version,"deployment_env":c.harness.deployment_env}})
}
#[test]
fn replays_runtime_link_config_and_backoff_fixtures() {
    let cases = load_contract("runtime-link/config-cases.json").unwrap();
    for case in cases["cases"].as_array().unwrap() {
        let env = obj(&case["env"]);
        let h = case
            .get("harness")
            .map(|v| rl::HarnessEnvelope {
                kind: v["kind"].as_str().unwrap_or("").into(),
                version: v["version"].as_str().unwrap_or("").into(),
                deployment_env: v["deployment_env"].as_str().unwrap_or("").into(),
            })
            .unwrap_or_default();
        let result = rl::config_from_env(&env, h);
        if case["expect"]["ok"] == true {
            assert_eq!(
                config_value(&result.unwrap()),
                case["expect"]["config"],
                "{}",
                case["name"]
            );
        } else {
            assert_eq!(
                result.unwrap_err().code,
                case["expect"]["error"].as_str().unwrap(),
                "{}",
                case["name"]
            );
        }
    }
    let cases = load_contract("runtime-link/backoff-cases.json").unwrap();
    for c in cases["cases"].as_array().unwrap() {
        assert_eq!(
            rl::backoff_delay_ms(
                c["min_ms"].as_u64().unwrap(),
                c["max_ms"].as_u64().unwrap(),
                c["attempt"].as_i64().unwrap(),
                c["rand"].as_f64().unwrap()
            ),
            c["delay_ms"].as_u64().unwrap()
        );
    }
}
#[test]
fn replays_runtime_link_enums_environment_and_exit_fixtures() {
    let enums = load_contract("runtime-link/enums.json").unwrap();
    for (key, values) in [
        ("harness_kinds", rl::HARNESS_KINDS),
        ("link_states", rl::LINK_STATES),
        ("failure_classes", rl::FAILURE_CLASSES),
        ("beat_outcomes", rl::BEAT_OUTCOMES),
        ("lifecycle_events", rl::LIFECYCLE_EVENTS),
        ("child_env_allowlist", rl::CHILD_ENV_ALLOWLIST),
    ] {
        assert_eq!(enums[key], json!(values), "{key}");
    }
    assert_eq!(enums["contract_version"], rl::CONTRACT_VERSION);
    assert_eq!(enums["max_child_line_bytes"], rl::MAX_CHILD_LINE_BYTES);
    assert_eq!(enums["event_buffer_size"], rl::EVENT_BUFFER_SIZE);
    let fixture = load_contract("runtime-link/child-env-allowlist.json").unwrap();
    let mut source = BTreeMap::new();
    for k in rl::CHILD_ENV_ALLOWLIST {
        source.insert((*k).into(), format!("value-{k}"));
    }
    for k in fixture["canaries"].as_array().unwrap() {
        source.insert(k.as_str().unwrap().into(), "CANARY_SECRET".into());
    }
    source.insert("LEAK_ME".into(), "secret".into());
    let env = rl::child_environment(&source, "0.1.0", &Default::default());
    assert!(env.len() <= fixture["env_count_max"].as_u64().unwrap() as usize);
    for c in fixture["canaries"].as_array().unwrap() {
        assert!(!env.contains_key(c.as_str().unwrap()));
    }
    assert_eq!(env["KEI_AGENTWARE_SDK_LANG"], "rust");
    assert_eq!(env["KEI_AGENTWARE_SDK_VERSION"], "0.1.0");
    assert!(!env.contains_key("LEAK_ME"));
    let exits = load_contract("runtime-link/exit-code-cases.json").unwrap();
    for c in exits["cases"].as_array().unwrap() {
        let action = rl::handle_child_exit(c["exit_code"].as_i64().unwrap() as i32);
        match action {
            rl::ExitAction::Clean => assert_eq!(c["failure_class"], ""),
            rl::ExitAction::Restart(class) | rl::ExitAction::Terminal(class) => {
                assert_eq!(c["failure_class"], class)
            }
        }
        assert_eq!(matches!(action, rl::ExitAction::Terminal(_)), c["terminal"]);
        assert_eq!(matches!(action, rl::ExitAction::Restart(_)), c["restart"]);
    }
}
#[test]
fn replays_runtime_link_lifecycle_redaction_fixture() {
    let fixture = load_contract("runtime-link/lifecycle-redaction.json").unwrap();
    for c in fixture["cases"].as_array().unwrap() {
        let got = rl::redact_lifecycle(&c["input"]);
        if c["expect"]["ok"] == true {
            assert_eq!(got.unwrap(), c["expect"]["serialized"], "{}", c["name"]);
        } else {
            assert!(got.is_err(), "{}", c["name"]);
        }
    }
}
#[test]
fn replays_every_runtime_link_wire_fixture() {
    let fixture = load_contract("runtime-link/wire/child-events.json").unwrap();
    for c in fixture["cases"].as_array().unwrap() {
        let line = c["line"].as_str().unwrap();
        let result = rl::parse_child_line(line.as_bytes());
        let expect = c["expect"]["kind"].as_str().unwrap();
        match expect {
            "contract_mismatch" => assert_eq!(
                result.unwrap_err(),
                rl::WireError::ContractMismatch,
                "{}",
                c["name"]
            ),
            "line_too_long" => assert_eq!(
                result.unwrap_err(),
                rl::WireError::LineTooLong,
                "{}",
                c["name"]
            ),
            "malformed" => assert_eq!(
                result.unwrap_err(),
                rl::WireError::Malformed,
                "{}",
                c["name"]
            ),
            kind => {
                let event = result.unwrap();
                let actual = match event {
                    rl::ChildEvent::Identity(v) => {
                        assert_eq!(
                            v["dropped_agents"],
                            c["expect"]
                                .get("dropped_agents")
                                .cloned()
                                .unwrap_or(json!(0)),
                            "{}",
                            c["name"]
                        );
                        json!({"kind":"identity","fields":v["fields"]})
                    }
                    rl::ChildEvent::Beat(v) => json!({"kind":"beat","fields":v}),
                    rl::ChildEvent::Terminal(v) => json!({"kind":"terminal","fields":v}),
                    rl::ChildEvent::Ignored => json!({"kind":"ignored"}),
                };
                assert_eq!(actual["kind"], kind, "{}", c["name"]);
                if c["expect"].get("fields").is_some() {
                    assert_eq!(actual["fields"], c["expect"]["fields"], "{}", c["name"]);
                }
            }
        }
    }
    for c in fixture["synthesized"].as_array().unwrap() {
        let bytes = c["bytes"].as_u64().unwrap() as usize;
        let prefix = "{\"v\":1,\"event\":\"progress\",\"pad\":\"";
        let suffix = "\"}";
        let line = format!(
            "{prefix}{}{suffix}",
            "x".repeat(bytes.saturating_sub(prefix.len() + suffix.len()))
        );
        let result = rl::parse_child_line(line.as_bytes());
        if c["expect"]["kind"] == "line_too_long" {
            assert_eq!(
                result.unwrap_err(),
                rl::WireError::LineTooLong,
                "{}",
                c["name"]
            )
        } else {
            assert!(result.is_ok(), "{}", c["name"]);
        }
    }
}

#[test]
fn builds_fixed_argv_for_both_child_processes() {
    let config = rl::Config {
        interval_s: 60,
        beat_timeout_s: 10,
        ..rl::Config::default()
    };
    let heartbeat = rl::build_command("kei-proxy", rl::ChildKind::Heartbeat, &config);
    assert_eq!(
        heartbeat
            .get_args()
            .map(|s| s.to_string_lossy().into_owned())
            .collect::<Vec<_>>(),
        [
            "runtime",
            "heartbeat",
            "--interval",
            "60s",
            "--timeout",
            "10s",
            "--output",
            "jsonl",
            "--parent-stdin"
        ]
    );
    let collector = rl::build_command("kei-proxy", rl::ChildKind::AuditCollector, &config);
    assert_eq!(
        collector
            .get_args()
            .map(|s| s.to_string_lossy().into_owned())
            .collect::<Vec<_>>(),
        ["collector"]
    );
}
