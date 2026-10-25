use agentware_kei::{
    evaluator::{Action, CallerContext, KeiProxyEvaluator, RULE},
    fixture::load_contract,
    runtimesocket::*,
};
use serde_json::{Map, Value, json};
use std::{
    collections::HashMap,
    io::{Read, Write},
    os::unix::net::UnixListener,
    path::PathBuf,
    sync::mpsc,
    thread,
    time::Duration,
};

#[test]
fn runtime_socket_constants_match_shared_contract() {
    let c = load_contract("runtime-socket/constants.json").unwrap();
    assert_eq!(c["socket"]["default_path"], DEFAULT_SOCKET_PATH);
    assert_eq!(c["socket"]["session_header"], SESSION_HEADER);
    assert_eq!(c["socket"]["session_not_found"], SESSION_NOT_FOUND);
    assert_eq!(c["socket"]["path_env"], "KEI_RUNTIME_SOCKET_PATH");
    assert_eq!(c["socket"]["token_env"], "KEI_RUNTIME_TOKEN");
    assert_eq!(c["socket"]["default_timeout_ms"], 10000);
    assert_eq!(c["schemas"]["session"], SESSION_SCHEMA);
    assert_eq!(c["schemas"]["authorize_request"], AUTHORIZE_REQUEST_SCHEMA);
    assert_eq!(
        c["schemas"]["authorize_response"],
        AUTHORIZE_RESPONSE_SCHEMA
    );
    assert_eq!(
        c["schemas"]["policy_bundle_status"],
        POLICY_BUNDLE_STATUS_SCHEMA
    );
    assert_eq!(c["routes"]["open_session"], json!(["PUT", "/v1/session"]));
    assert_eq!(
        c["routes"]["close_session"],
        json!(["DELETE", "/v1/session"])
    );
    assert_eq!(c["routes"]["authorize"], json!(["POST", "/v1/authorize"]));
    assert_eq!(
        c["routes"]["policy_bundle_status"],
        json!(["GET", "/v1/policy-bundle/status"])
    );
    assert_eq!(c["socket"]["default_model_timeout_ms"], 120000);
    assert_eq!(c["routes"]["list_models"], json!(["GET", "/v1/models"]));
    assert_eq!(
        c["routes"]["chat_completion"],
        json!(["POST", "/v1/chat/completions"])
    );
    assert_eq!(
        c["routes"]["release_credential"],
        json!(["POST", "/v1/credentials:release"])
    );
    assert_eq!(
        c["schemas"]["credential_release_request"],
        "kei.credential-release-request/v1"
    );
    assert_eq!(
        c["schemas"]["credential_release_response"],
        "kei.credential-release-response/v1"
    );
}

fn serve(path: PathBuf, exchanges: Vec<Value>) -> mpsc::Receiver<Value> {
    let listener = UnixListener::bind(path).unwrap();
    let (tx, rx) = mpsc::channel();
    thread::spawn(move || {
        for ex in exchanges {
            let (mut s, _) = listener.accept().unwrap();
            let mut buf = Vec::new();
            let mut tmp = [0; 4096];
            let body_start;
            loop {
                let n = s.read(&mut tmp).unwrap();
                buf.extend_from_slice(&tmp[..n]);
                if let Some(p) = buf.windows(4).position(|w| w == b"\r\n\r\n") {
                    body_start = p + 4;
                    let head = String::from_utf8_lossy(&buf[..p]);
                    let len = head
                        .lines()
                        .find_map(|l| {
                            l.to_ascii_lowercase()
                                .strip_prefix("content-length: ")
                                .and_then(|v| v.parse::<usize>().ok())
                        })
                        .unwrap_or(0);
                    while buf.len() < body_start + len {
                        let n = s.read(&mut tmp).unwrap();
                        buf.extend_from_slice(&tmp[..n]);
                    }
                    break;
                }
            }
            let head = String::from_utf8_lossy(&buf[..body_start - 4]);
            let session = head
                .lines()
                .find_map(|l| l.strip_prefix("X-Kei-Session: "))
                .unwrap_or("");
            let body_len = buf.len() - body_start;
            let body = if body_len == 0 {
                Value::Null
            } else {
                serde_json::from_slice(&buf[body_start..]).unwrap()
            };
            tx.send(json!({"method":head.split_whitespace().next().unwrap(),"path":head.split_whitespace().nth(1).unwrap(),"session":session,"body":body})).unwrap();
            let resp = &ex["response"];
            if resp["stall"] == true {
                thread::sleep(Duration::from_millis(450));
                continue;
            }
            let status = resp["status"].as_u64().unwrap_or(200);
            let payload = if let Some(raw) = resp["raw"].as_str() {
                raw.as_bytes().to_vec()
            } else if resp.get("body").is_some() {
                serde_json::to_vec(&resp["body"]).unwrap()
            } else {
                Vec::new()
            };
            let content_type = resp["content_type"].as_str().unwrap_or("application/json");
            let wire = format!(
                "HTTP/1.1 {status} Result\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                payload.len()
            );
            s.write_all(wire.as_bytes()).unwrap();
            s.write_all(&payload).unwrap();
        }
    });
    rx
}

#[test]
fn runtime_socket_supported_client_cases_replay() {
    let fixture = load_contract("runtime-socket/client-cases.json").unwrap();
    let token = fixture["token"].as_str().unwrap();
    let mut unsupported = Vec::new();
    for case in fixture["cases"].as_array().unwrap() {
        let name = case["name"].as_str().unwrap();
        if case["steps"].as_array().unwrap().iter().any(|s| {
            matches!(
                s["op"].as_str(),
                Some("list_models" | "chat_completion" | "stream_chat_completion")
            )
        }) {
            unsupported.push(name.to_owned());
            continue;
        }
        let path =
            std::env::temp_dir().join(format!("kei-{}-{}.sock", std::process::id(), name.len()));
        let _ = std::fs::remove_file(&path);
        let exchanges = case["exchanges"].as_array().unwrap().clone();
        let daemon = case["daemon"] != "absent";
        let rx = if daemon {
            Some(serve(path.clone(), exchanges.clone()))
        } else {
            None
        };
        let env = case
            .get("env")
            .and_then(Value::as_object)
            .map(|m| {
                m.iter()
                    .map(|(k, v)| (k.clone(), v.as_str().unwrap_or("").to_owned()))
                    .collect()
            })
            .unwrap_or_else(|| HashMap::from([("KEI_RUNTIME_TOKEN".into(), token.into())]));
        let mut c = Client::new(path.clone());
        c.timeout = Duration::from_millis(case["timeout_ms"].as_u64().unwrap_or(10000));
        c.env = env;
        let mut got = Vec::new();
        for step in case["steps"].as_array().unwrap() {
            let a = &step["args"];
            let op = step["op"].as_str().unwrap();
            let result: Result<Value, SocketError> = match op {
                "open_session" => c
                    .open_session(Identity {
                        source: a["source"].as_str().unwrap_or("").into(),
                        external_id: a["external_id"].as_str().unwrap_or("").into(),
                    })
                    .map(|s| json!({"session_id":s.session_id})),
                "close_session" => c
                    .close_session(&Session {
                        source: a["session"]["source"].as_str().unwrap_or("").into(),
                        external_id: a["session"]["external_id"].as_str().unwrap_or("").into(),
                        session_id: a["session"]["session_id"].as_str().unwrap_or("").into(),
                    })
                    .map(|_| Value::Null),
                "forget_session" => {
                    c.forget_session(&Identity {
                        source: a["source"].as_str().unwrap_or("").into(),
                        external_id: a["external_id"].as_str().unwrap_or("").into(),
                    });
                    Ok(Value::Null)
                }
                "authorize" => {
                    let mut f = Map::new();
                    if let Some(m) = a["request"].as_object() {
                        f.extend(m.clone());
                    }
                    c.authorize(
                        &Session {
                            source: a["session"]["source"].as_str().unwrap_or("").into(),
                            external_id: a["session"]["external_id"].as_str().unwrap_or("").into(),
                            session_id: a["session"]["session_id"].as_str().unwrap_or("").into(),
                        },
                        &f,
                    )
                }
                "policy_bundle_status" => c.policy_bundle_status(),
                _ => unreachable!(),
            };
            let expect = &step["expect"];
            if expect.get("invalid_argument") == Some(&Value::Bool(true)) {
                assert_eq!(
                    result.unwrap_err().kind,
                    ErrorKind::InvalidArgument,
                    "{name}"
                );
            } else if expect.get("error").is_some() {
                let e = result.unwrap_err();
                let class = expect["error"]["reason_class"].as_str().unwrap();
                let actual = match e.kind {
                    ErrorKind::ProxyUnavailable => "proxy_unavailable",
                    ErrorKind::ProxyTimeout => "proxy_timeout",
                    ErrorKind::ProxyError => "proxy_error",
                    ErrorKind::MalformedResponse => "malformed_response",
                    ErrorKind::MissingToken => "missing_token",
                    ErrorKind::Cancelled => "cancelled",
                    ErrorKind::InvalidArgument => "invalid_argument",
                };
                assert_eq!(actual, class, "{name}");
                assert_eq!(
                    e.code,
                    expect["error"]["code"].as_str().unwrap_or(""),
                    "{name}"
                );
                assert!(!e.to_string().contains(token));
            } else {
                assert_eq!(result.unwrap(), expect["result"], "{name}");
            }
        }
        if let Some(rx) = rx {
            for ex in exchanges {
                got.push(rx.recv_timeout(Duration::from_secs(2)).unwrap());
                assert_eq!(got.last().unwrap(), &ex["request"], "{name}");
            }
        }
        let _ = std::fs::remove_file(path);
    }
    assert_eq!(
        unsupported.len(),
        10,
        "unexpected retired-route fixture cases: {unsupported:?}"
    );
}

use agentware_kei::runtimelink as rl;
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
#[test]
fn evaluator_replays_shared_authorize_responses() {
    let fixture = load_contract("runtime-socket/client-cases.json").unwrap();
    let cases = fixture["cases"].as_array().unwrap();
    for (case_name, expected_action, expected_code) in [
        (
            "authorize_sends_supported_set_fields_and_returns_the_permit",
            Action::Allow,
            None,
        ),
        (
            "authorize_returns_a_deny_with_its_enrollment",
            Action::Deny,
            Some("subject_unlinked"),
        ),
        (
            "authorize_returns_a_deny_with_preflight_required",
            Action::Deny,
            Some("preflight_required"),
        ),
    ] {
        let case = cases
            .iter()
            .find(|c| c["name"] == case_name)
            .unwrap_or_else(|| panic!("fixture case {case_name} missing"));
        let path = std::env::temp_dir().join(format!(
            "kei-evaluator-{}-{}.sock",
            std::process::id(),
            case_name.len()
        ));
        let _ = std::fs::remove_file(&path);
        let mut exchanges = vec![json!({
            "response": {"status": 201, "body": {
                "schema": SESSION_SCHEMA,
                "session_id": "sess-1",
                "user_id": "ada",
                "status": "linked"
            }}
        })];
        exchanges.extend(case["exchanges"].as_array().unwrap().iter().cloned());
        let rx = serve(path.clone(), exchanges);
        let mut client = Client::new(&path);
        client.env.insert(
            "KEI_RUNTIME_TOKEN".into(),
            fixture["token"].as_str().unwrap().into(),
        );
        let evaluator = KeiProxyEvaluator::new(client);
        let decision = evaluator.evaluate(
            "github.get_issue",
            &json!({"owner":"acme", "repo":"widgets", "issue_number":7}),
            &CallerContext {
                user_id: "ada".into(),
                invoking_subject: "ada".into(),
                source: "openwebui".into(),
                metadata: HashMap::from([("framework".into(), "openwebui".into())]),
                ..CallerContext::default()
            },
        );
        assert_eq!(decision.action, expected_action, "{case_name}");
        assert_eq!(
            decision.reason_code.as_deref(),
            expected_code,
            "{case_name}"
        );
        assert_eq!(
            decision.rule,
            if expected_action == Action::Allow {
                "p-allow"
            } else {
                RULE
            }
        );
        let opened = rx.recv_timeout(Duration::from_secs(2)).unwrap();
        assert_eq!(opened["method"], "PUT", "{case_name}");
        assert_eq!(opened["path"], "/v1/session", "{case_name}");
        for ex in case["exchanges"].as_array().unwrap() {
            let got = rx.recv_timeout(Duration::from_secs(2)).unwrap();
            assert_eq!(got["method"], ex["request"]["method"], "{case_name}");
            assert_eq!(got["path"], ex["request"]["path"], "{case_name}");
            assert_eq!(got["session"], "sess-1", "{case_name}");
        }
        let _ = std::fs::remove_file(path);
    }
}
