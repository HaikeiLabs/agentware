use agentware_kei::{
    evaluator::{Action, CallerContext, KeiProxyEvaluator},
    middleware::{AuditEvent, Auditor, Middleware, ToolExecutor},
    runtimesocket::{
        AUTHORIZE_RESPONSE_SCHEMA, Client, Identity, POLICY_BUNDLE_STATUS_SCHEMA, SESSION_SCHEMA,
    },
    toolspec::{HarnessExecutor, Route, ToolSpec},
    toolsync,
};
use serde_json::{Value, json};
use std::{
    collections::HashMap,
    fs,
    io::{Read, Write},
    os::unix::net::{UnixListener, UnixStream},
    path::PathBuf,
    sync::{
        Arc, Mutex,
        atomic::{AtomicUsize, Ordering},
    },
    thread,
    time::Duration,
};

const TOKEN: &str = "offline-test-token";
const SESSION_ID: &str = "fake-session-id";

#[derive(Clone)]
struct RecordingAuditor(Arc<Mutex<Vec<AuditEvent>>>);

impl Auditor for RecordingAuditor {
    fn record(&self, event: AuditEvent) {
        self.0.lock().unwrap().push(event);
    }
}

struct CountingExecutor(Arc<AtomicUsize>);

impl ToolExecutor for CountingExecutor {
    fn execute(&self, tool: &str, _args: &Value) -> Result<Value, String> {
        self.0.fetch_add(1, Ordering::SeqCst);
        Ok(json!({"executed":tool}))
    }
}

struct Request {
    method: String,
    path: String,
    session: String,
    body: Value,
}

fn response(status: u16, value: Value) -> Vec<u8> {
    let body = serde_json::to_vec(&value).unwrap();
    let head = format!(
        "HTTP/1.1 {status} Result\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()
    );
    [head.as_bytes(), &body].concat()
}

fn read_request(stream: &mut UnixStream) -> Request {
    let mut bytes = Vec::new();
    let mut chunk = [0; 4096];
    let (header_end, content_length) = loop {
        let count = stream.read(&mut chunk).unwrap();
        assert_ne!(count, 0, "client closed before request headers");
        bytes.extend_from_slice(&chunk[..count]);
        if let Some(end) = bytes.windows(4).position(|w| w == b"\r\n\r\n") {
            let headers = String::from_utf8_lossy(&bytes[..end]);
            let length = headers
                .lines()
                .find_map(|line| {
                    line.to_ascii_lowercase()
                        .strip_prefix("content-length: ")
                        .and_then(|s| s.parse::<usize>().ok())
                })
                .unwrap_or(0);
            break (end + 4, length);
        }
    };
    while bytes.len() < header_end + content_length {
        let count = stream.read(&mut chunk).unwrap();
        assert_ne!(count, 0, "client closed before request body");
        bytes.extend_from_slice(&chunk[..count]);
    }
    let headers = String::from_utf8_lossy(&bytes[..header_end - 4]);
    let mut parts = headers.lines().next().unwrap().split_whitespace();
    let method = parts.next().unwrap().to_string();
    let path = parts.next().unwrap().to_string();
    let session = headers
        .lines()
        .find_map(|line| line.strip_prefix("X-Kei-Session: "))
        .unwrap_or("")
        .to_string();
    let body = if content_length == 0 {
        Value::Null
    } else {
        serde_json::from_slice(&bytes[header_end..header_end + content_length]).unwrap()
    };
    Request {
        method,
        path,
        session,
        body,
    }
}

fn serve(
    path: PathBuf,
    authorize_answers: Vec<Value>,
    request_count: usize,
) -> (thread::JoinHandle<()>, Arc<Mutex<Vec<Request>>>) {
    let listener = UnixListener::bind(path).unwrap();
    listener.set_nonblocking(false).unwrap();
    let requests = Arc::new(Mutex::new(Vec::new()));
    let captured = requests.clone();
    let handle = thread::spawn(move || {
        let mut answers = authorize_answers.into_iter();
        let mut opened = 0usize;
        for _ in 0..request_count {
            let (mut stream, _) = listener.accept().unwrap();
            stream
                .set_read_timeout(Some(Duration::from_secs(2)))
                .unwrap();
            let request = read_request(&mut stream);
            let payload = match (request.method.as_str(), request.path.as_str()) {
                ("GET", "/readyz") => json!({"status":"ready"}),
                ("GET", "/v1/policy-bundle/status") => json!({
                    "schema":POLICY_BUNDLE_STATUS_SCHEMA,
                    "bundle_schema":"policy.v1"
                }),
                ("PUT", "/v1/session") => {
                    opened += 1;
                    json!({"schema":SESSION_SCHEMA,"session_id":format!("{SESSION_ID}-{opened}")})
                }
                ("DELETE", "/v1/session") => json!({"closed":true}),
                ("POST", "/v1/authorize") => answers
                    .next()
                    .expect("missing scripted authorization answer"),
                ("POST", "/v1/tools:register") => json!({"registered":true}),
                _ => panic!("unexpected route: {} {}", request.method, request.path),
            };
            let status = if payload.pointer("/error/code").and_then(Value::as_str)
                == Some("session_not_found")
            {
                404
            } else {
                200
            };
            captured.lock().unwrap().push(request);
            stream.write_all(&response(status, payload)).unwrap();
        }
    });
    (handle, requests)
}

fn permit() -> Value {
    json!({"schema":AUTHORIZE_RESPONSE_SCHEMA,"decision":"permit","policy_id":"demo-policy"})
}

fn deny() -> Value {
    json!({"schema":AUTHORIZE_RESPONSE_SCHEMA,"decision":"deny","reason":"blocked","policy_id":"demo-policy"})
}

fn not_found() -> Value {
    json!({"error":{"code":"session_not_found","message":"session_not_found"}})
}

fn client(path: PathBuf) -> Client {
    let mut client = Client::new(path);
    client.env.insert("KEI_RUNTIME_TOKEN".into(), TOKEN.into());
    client.timeout = Duration::from_secs(2);
    client
}

fn temp_socket(label: &str) -> PathBuf {
    PathBuf::from(format!("/tmp/aw-{}-{label}.sock", std::process::id()))
}

fn tiny_tool() -> ToolSpec {
    ToolSpec {
        name: "demo.read".into(),
        description: "Read a demo record".into(),
        parameters: None,
        service: "demo".into(),
        source: "e2e-test".into(),
        operation_class: "read".into(),
        route: Route::HarnessExecutor(HarnessExecutor {
            executor: "demo".into(),
            registration: "demo.read".into(),
        }),
        required_capabilities: vec![],
        resource_types: vec![],
        enabled: Some(true),
    }
}

#[test]
fn governed_tool_flow_uses_fake_unix_daemon_and_fails_closed() {
    let socket = temp_socket("flow");
    let _ = fs::remove_file(&socket);
    // Requests: ready, policy status, initial session, allow, deny, manifest, stale-session
    // authorization, replacement session, and the retried authorization.
    let (daemon, captured) = serve(
        socket.clone(),
        vec![permit(), deny(), not_found(), permit()],
        9,
    );
    let mut runtime = client(socket.clone());
    runtime.readyz().unwrap();
    let policy_status = runtime.policy_bundle_status().unwrap();
    assert_eq!(policy_status["schema"], POLICY_BUNDLE_STATUS_SCHEMA);
    let identity = Identity {
        source: "example".into(),
        external_id: "user-1".into(),
    };
    let session = runtime.open_session(identity).unwrap();
    let executions = Arc::new(AtomicUsize::new(0));
    let events = Arc::new(Mutex::new(Vec::new()));
    let caller = CallerContext {
        user_id: "user-1".into(),
        invoking_subject: "user-1".into(),
        session_id: session.session_id.clone(),
        source: "example".into(),
        metadata: HashMap::from([("framework".into(), "example".into())]),
        ..CallerContext::default()
    };
    let middleware = Middleware::new(
        CountingExecutor(executions.clone()),
        KeiProxyEvaluator::new(runtime.clone()),
        RecordingAuditor(events.clone()),
    );
    let args = json!({"action":"read","resource":"synthetic-record","private":"RAW-ARGUMENT-MUST-NOT-LEAK"});
    assert!(middleware.execute("demo.read", &args, &caller).success);
    assert!(
        !middleware
            .execute(
                "demo.write",
                &json!({"action":"write","private":"DENY-ARGUMENT"}),
                &caller
            )
            .success
    );
    assert_eq!(
        executions.load(Ordering::SeqCst),
        1,
        "denied tool must not run"
    );
    toolsync::register(&runtime, &[tiny_tool()]).unwrap();
    assert!(
        middleware.execute("demo.read", &args, &caller).success,
        "stale session should reopen once and retry authorization"
    );
    daemon.join().unwrap();

    let requests = captured.lock().unwrap();
    assert_eq!(
        requests
            .iter()
            .filter(|r| r.path == "/v1/session" && r.method == "PUT")
            .count(),
        2,
        "session_not_found should reopen exactly once"
    );
    assert_eq!(
        requests
            .iter()
            .filter(|r| r.path == "/v1/tools:register")
            .count(),
        1
    );
    assert!(requests.iter().any(|r| r.path == "/readyz"));
    assert!(
        requests
            .iter()
            .any(|r| r.path == "/v1/policy-bundle/status")
    );
    let manifest = requests
        .iter()
        .find(|r| r.path == "/v1/tools:register")
        .unwrap();
    assert_eq!(manifest.body["schema"], "kei.tool-manifest/v3");
    assert_eq!(manifest.body["tools"].as_array().unwrap().len(), 1);
    assert!(
        requests
            .iter()
            .filter(|r| r.path == "/v1/authorize")
            .all(|r| {
                r.body["schema"] == "kei.authorize-request/v1"
                    && r.body.get("private").is_none()
                    && r.body["tool_args_digest"].is_string()
            })
    );
    assert!(
        requests
            .iter()
            .filter(|r| r.path == "/v1/authorize")
            .all(|r| r.session == SESSION_ID.to_owned() + "-1"
                || r.session == SESSION_ID.to_owned() + "-2")
    );
    let audit_debug = format!("{:?}", events.lock().unwrap());
    assert!(!audit_debug.contains("RAW-ARGUMENT-MUST-NOT-LEAK"));
    assert!(!audit_debug.contains("DENY-ARGUMENT"));
    assert_eq!(events.lock().unwrap().len(), 3);
    assert_eq!(events.lock().unwrap()[1].decision, Action::Deny);

    // A configured but absent socket is a refusal; middleware audits the deny
    // and never invokes the executor.
    let down_path = temp_socket("down");
    let _ = fs::remove_file(&down_path);
    let down_exec = Arc::new(AtomicUsize::new(0));
    let down_audit = Arc::new(Mutex::new(Vec::new()));
    let down_caller = CallerContext {
        user_id: "u".into(),
        invoking_subject: "u".into(),
        source: "example".into(),
        ..CallerContext::default()
    };
    let down_middleware = Middleware::new(
        CountingExecutor(down_exec.clone()),
        KeiProxyEvaluator::new(client(down_path)),
        RecordingAuditor(down_audit.clone()),
    );
    let result = down_middleware.execute(
        "demo.read",
        &json!({"private":"offline-down-secret"}),
        &down_caller,
    );
    assert!(!result.success);
    assert_eq!(down_exec.load(Ordering::SeqCst), 0);
    assert_eq!(down_audit.lock().unwrap().len(), 1);
    assert!(!format!("{:?}", down_audit.lock().unwrap()).contains("offline-down-secret"));
    let _ = fs::remove_file(socket);
}
