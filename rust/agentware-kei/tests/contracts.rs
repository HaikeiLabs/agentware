use agentware_kei::{fixture::load_contract, runtimesocket::*};
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
