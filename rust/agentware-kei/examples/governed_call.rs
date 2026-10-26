//! Run one permitted and one denied governed tool call through `kei-proxy serve`.
//! The SDK treats `source` and `external_id` as opaque strings chosen by the
//! harness; the daemon resolves them to a Kei user.

use agentware_kei::{
    evaluator::{CallerContext, KeiProxyEvaluator},
    middleware::{AuditEvent, Auditor, Middleware, ToolExecutor},
    runtimesocket::{Client, Identity},
    toolspec::{HarnessExecutor, Route, ToolSpec},
    toolsync,
};
use serde_json::{Value, json};
use std::{
    collections::HashMap,
    env,
    path::PathBuf,
    process,
    sync::{Arc, Mutex},
};

struct DemoExecutor;
struct DemoAuditor(Arc<Mutex<Vec<AuditEvent>>>);

impl Auditor for DemoAuditor {
    fn record(&self, event: AuditEvent) {
        self.0.lock().expect("audit mutex poisoned").push(event);
    }
}

impl ToolExecutor for DemoExecutor {
    fn execute(&self, tool: &str, _args: &Value) -> Result<Value, String> {
        Ok(json!({"tool": tool, "status": "executed"}))
    }
}

fn main() {
    if let Err(error) = run() {
        eprintln!("governed_call failed: {error}");
        process::exit(1);
    }
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let mut socket = env::var_os("KEI_RUNTIME_SOCKET_PATH").map(PathBuf::from);
    let mut source = None;
    let mut external_id = None;
    let mut args = env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--socket" => {
                socket = Some(PathBuf::from(args.next().ok_or("missing --socket value")?))
            }
            "--source" => source = Some(args.next().ok_or("missing --source value")?),
            "--external-id" => {
                external_id = Some(args.next().ok_or("missing --external-id value")?)
            }
            _ => return Err(format!("unknown argument: {arg}").into()),
        }
    }
    let socket = socket.ok_or("pass --socket or set KEI_RUNTIME_SOCKET_PATH")?;
    let source = source.ok_or("pass --source")?;
    let external_id = external_id.ok_or("pass --external-id")?;

    let mut client = Client::new(socket);
    let ready = client.readyz()?;
    let session = client.open_session(Identity {
        source: source.clone(),
        external_id: external_id.clone(),
    })?;
    println!(
        "socket ready: {}",
        ready.get("status").and_then(Value::as_str).unwrap_or("yes")
    );
    println!(
        "session opened: {}…",
        &session.session_id[..session.session_id.len().min(8)]
    );

    let evaluator = KeiProxyEvaluator::new(client.clone());
    let audit_events = Arc::new(Mutex::new(Vec::new()));
    let middleware = Middleware::new(DemoExecutor, evaluator, DemoAuditor(audit_events.clone()));
    let caller = CallerContext {
        user_id: external_id.clone(),
        invoking_subject: external_id,
        session_id: session.session_id,
        source,
        metadata: HashMap::from([("framework".into(), "example".into())]),
        ..CallerContext::default()
    };
    let permitted = middleware.execute(
        "demo.read",
        &json!({"action":"read","resource":"demo"}),
        &caller,
    );
    if !permitted.success {
        return Err("demo.read was not permitted".into());
    }
    println!("permit: demo.read executed");
    let denied = middleware.execute(
        "demo.write",
        &json!({"action":"write","resource":"demo"}),
        &caller,
    );
    if denied.success {
        return Err("demo.write unexpectedly executed".into());
    }
    println!("deny: demo.write blocked");

    toolsync::register(
        &client,
        &[ToolSpec {
            name: "demo.read".into(),
            description: "Read a demonstration record".into(),
            parameters: None,
            service: "demo".into(),
            source: "agentware-example".into(),
            operation_class: "read".into(),
            route: Route::HarnessExecutor(HarnessExecutor {
                executor: "demo".into(),
                registration: "demo.read".into(),
            }),
            required_capabilities: vec![],
            resource_types: vec![],
            enabled: Some(true),
        }],
    )?;
    println!("tool manifest synced: 1 tool");
    println!(
        "audit events emitted: {} (one per tool call; metadata only)",
        audit_events.lock().expect("audit mutex poisoned").len()
    );
    Ok(())
}
