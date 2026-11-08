//! `kei-proxy authorize` subprocess client — the CLI fallback transport.
//!
//! Port of `go/kei/evaluator/client.go`. The runtime token reaches the child
//! only through its environment, never argv, and the child's stderr is
//! discarded: neither may surface in errors or debug output.

use std::collections::HashMap;
use std::fs::{self, OpenOptions};
use std::io::{ErrorKind as IoErrorKind, Read};
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, Instant};

use serde_json::Value;
use sha2::{Digest, Sha256};

use super::{
    AuthorizationClient, AuthorizeRequest, ClientError, REASON_EMPTY_RESPONSE,
    REASON_EXIT_MISMATCH, REASON_MALFORMED_RESPONSE, REASON_MISSING_TOKEN, REASON_PIN_MISMATCH,
    REASON_PROXY_ERROR, REASON_PROXY_TIMEOUT, REASON_PROXY_UNAVAILABLE,
};

/// Carries the runtime installation credential to kei-proxy.
pub const RUNTIME_TOKEN_ENV: &str = "KEI_RUNTIME_TOKEN";

/// Bounds one authorize call when [`CliClient::timeout`] is zero.
pub const DEFAULT_TIMEOUT: Duration = Duration::from_secs(10);

/// Bounds how much authorize output is read.
const MAX_STDOUT: usize = 1 << 20;

/// Bounds how much of a pinned executable is hashed.
const MAX_EXECUTABLE: u64 = 256 << 20;

/// How often a running child is polled for exit while waiting.
const POLL_INTERVAL: Duration = Duration::from_millis(5);

/// Grace period for the stdout pipe to drain once the child is gone
/// (Go: cmd.WaitDelay).
const WAIT_DELAY: Duration = Duration::from_millis(100);

const PROXY_INSTALL_COMMAND: &str =
    r#"curl -fsSL https://haikeilabs.com/install.sh | sh -s -- --proxy-only -d "$HOME/.local/bin""#;

/// Lists the parent variables the kei-proxy child may inherit. PATH and HOME
/// are deliberately absent, as are KEI_PROXY_* identity variables: identity
/// travels as flags.
pub const AUTHORIZE_CHILD_ENV_ALLOWLIST: &[&str] = &[
    "TZ",
    RUNTIME_TOKEN_ENV,
    "KEI_RUNTIME_CONTROL_PLANE_URL",
    "KEI_RUNTIME_VERSION",
    "KEI_API_URL",
    "KEI_PROXY_REGISTRY",
    "KEI_PROXY_AUDIT",
    "KEI_PROXY_AUDIT_MAX_BYTES",
    "KEI_PROXY_PROVIDER_USER",
    "KEI_CREDENTIAL_STORE_INSTALLATION_ID",
    "KEI_AWS_SECRETS_MANAGER_ENDPOINT",
];

/// Runs `kei-proxy authorize` once per call — the CLI fallback transport
/// (Go: `evaluator.CLIClient`). The runtime token reaches the child only
/// through its environment, never argv.
///
/// The child sees only the allowlisted environment (no PATH, no HOME), so the
/// executable is resolved to an absolute path in the parent. The child runs in
/// its own process group, and a timeout SIGKILLs the whole group. With
/// `expected_sha256` set, the binary is re-proved before every spawn: the
/// path must equal its realpath, it is opened with `O_NOFOLLOW`, must be a
/// regular file, and its SHA-256 is read from that descriptor and compared in
/// constant time; any drift denies with the pin_mismatch class without
/// spawning. What remains is the hash-then-exec race; close it by making the
/// binary's directory and its ancestors writable only by the deploy owner.
#[derive(Debug, Clone)]
pub struct CliClient {
    /// Path or name of the kei-proxy binary. Default "kei-proxy". A bare name
    /// is looked up in the absolute entries of `env`'s PATH.
    pub executable: PathBuf,
    /// Pins the binary to this SHA-256 (64 hex digits). The executable must
    /// then be an absolute, symlink-free path; a malformed pin or a relative
    /// path denies every call with the pin_mismatch class.
    pub expected_sha256: Option<String>,
    /// Bounds one call. Default [`DEFAULT_TIMEOUT`].
    pub timeout: Duration,
    /// The parent environment allowlisted variables are drawn from. None
    /// means the process environment.
    pub env: Option<HashMap<String, String>>,
}

impl AuthorizationClient for CliClient {
    fn authorize(&mut self, req: &AuthorizeRequest) -> Result<Value, ClientError> {
        let env = self.child_env();
        if env
            .get(RUNTIME_TOKEN_ENV)
            .is_none_or(|token| token.is_empty())
        {
            return Err(ClientError::new(
                REASON_MISSING_TOKEN,
                format!("{RUNTIME_TOKEN_ENV} is not set"),
            ));
        }
        let name = self.executable_name();
        if let Some(pin) = self
            .expected_sha256
            .as_deref()
            .filter(|pin| !pin.is_empty())
        {
            prove_executable(&name, pin)?;
        }
        let parent_path = self.lookup_parent("PATH").unwrap_or_default();
        let Some(executable) = resolve_executable(&name, &parent_path) else {
            return Err(ClientError::new(
                REASON_PROXY_UNAVAILABLE,
                format!(
                    "kei-proxy was not found on PATH (tried: {}); install with: {PROXY_INSTALL_COMMAND}",
                    executable_candidates(&name, &parent_path).join(", "),
                ),
            ));
        };
        let timeout = if self.timeout.is_zero() {
            DEFAULT_TIMEOUT
        } else {
            self.timeout
        };
        run_authorize(&executable, req, &env, timeout)
    }
}

impl CliClient {
    /// The executable path or name to run, defaulting to "kei-proxy".
    fn executable_name(&self) -> PathBuf {
        if self.executable.as_os_str().is_empty() {
            PathBuf::from("kei-proxy")
        } else {
            self.executable.clone()
        }
    }

    /// Reads one parent variable: the configured environment, else the
    /// process environment (Go: lookupParent).
    fn lookup_parent(&self, key: &str) -> Option<String> {
        match &self.env {
            Some(env) => env.get(key).cloned(),
            None => std::env::var(key).ok(),
        }
    }

    /// The exact environment the kei-proxy child receives.
    fn child_env(&self) -> HashMap<String, String> {
        let mut env = HashMap::new();
        for key in AUTHORIZE_CHILD_ENV_ALLOWLIST {
            if let Some(value) = self.lookup_parent(key) {
                env.insert((*key).to_string(), value);
            }
        }
        env
    }
}

/// Spawns the child in its own process group, waits with a deadline, and
/// SIGKILLs the whole group on timeout (Go: startInNewGroup + CommandContext).
fn run_authorize(
    executable: &Path,
    req: &AuthorizeRequest,
    env: &HashMap<String, String>,
    timeout: Duration,
) -> Result<Value, ClientError> {
    let could_not_start = || {
        ClientError::new(
            REASON_PROXY_UNAVAILABLE,
            format!(
                "could not start kei-proxy at {}; install with: {PROXY_INSTALL_COMMAND}",
                executable.display(),
            ),
        )
    };
    let mut command = Command::new(executable);
    command.args(authorize_argv(req));
    command.env_clear();
    for (key, value) in env {
        command.env(key, value);
    }
    command.stdin(Stdio::null());
    command.stdout(Stdio::piped());
    // stderr is free-form and may carry secrets: it is discarded.
    command.stderr(Stdio::null());
    // The child runs in its own process group; a timeout SIGKILLs the whole
    // group.
    command.process_group(0);

    let mut child = command.spawn().map_err(|_| could_not_start())?;
    let mut stdout = child.stdout.take().expect("stdout is piped");
    let (sender, receiver) = mpsc::channel();
    let reader = thread::spawn(move || {
        let _ = sender.send(read_limited(&mut stdout, MAX_STDOUT));
    });

    let deadline = Instant::now() + timeout;
    let mut timed_out = false;
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break Some(status),
            Ok(None) => {
                if Instant::now() >= deadline {
                    timed_out = true;
                    kill_process_group(child.id());
                    break child.wait().ok();
                }
                thread::sleep(POLL_INTERVAL);
            }
            // A post-spawn wait failure is treated like a failed start.
            Err(_) => return Err(could_not_start()),
        }
    };
    // WaitDelay: give the pipe a moment to drain once the child is gone.
    let (bytes, overflow) = match receiver.recv_timeout(WAIT_DELAY) {
        Ok(result) => {
            let _ = reader.join();
            result
        }
        // A process outside the killed group still holds the pipe; detach
        // rather than hang.
        Err(_) => (Vec::new(), true),
    };

    if timed_out {
        return Err(ClientError::new(
            REASON_PROXY_TIMEOUT,
            format!("no answer within {timeout:?}"),
        ));
    }
    let Some(exit_code) = status.and_then(|status| status.code()) else {
        return Err(ClientError::new(
            REASON_PROXY_ERROR,
            "kei-proxy terminated by a signal",
        ));
    };
    if overflow {
        return Err(ClientError::new(
            REASON_MALFORMED_RESPONSE,
            "stdout exceeded the size limit",
        ));
    }
    parse_authorize_output(&bytes, exit_code)
}

/// SIGKILLs the child's whole process group. The child was started with
/// `process_group(0)`, so the group id is its pid. std offers no safe kill(2)
/// and the crate forbids unsafe code, so the POSIX kill utility does it.
fn kill_process_group(pid: u32) {
    let _ = Command::new("/bin/sh")
        .arg("-c")
        .arg(format!("kill -9 -{pid} 2>/dev/null"))
        .status();
}

/// Reads stdout to EOF, keeping at most `limit` bytes and recording whether
/// more arrived (Go: limitedBuffer).
fn read_limited(reader: &mut impl Read, limit: usize) -> (Vec<u8>, bool) {
    let mut out = Vec::new();
    let mut total = 0usize;
    let mut overflow = false;
    let mut chunk = [0u8; 8192];
    loop {
        match reader.read(&mut chunk) {
            Ok(0) => return (out, overflow),
            Ok(n) => {
                total += n;
                if total > limit {
                    overflow = true;
                }
                if out.len() < limit {
                    let take = n.min(limit - out.len());
                    out.extend_from_slice(&chunk[..take]);
                }
            }
            Err(ref error) if error.kind() == IoErrorKind::Interrupted => {}
            Err(_) => return (out, overflow),
        }
    }
}

/// Returns an absolute path for `name`. Names containing a separator resolve
/// against the working directory; bare names are looked up in the absolute
/// entries of `path_list` (relative entries are skipped).
fn resolve_executable(name: &Path, path_list: &str) -> Option<PathBuf> {
    if name.is_absolute() {
        return Some(name.to_path_buf());
    }
    let name_str = name.to_string_lossy();
    if name_str.contains('/') {
        return if name_str.starts_with('/') {
            Some(name.to_path_buf())
        } else {
            std::env::current_dir().ok().map(|cwd| cwd.join(name))
        };
    }
    for dir in path_list.split(':') {
        if dir.is_empty() || !Path::new(dir).is_absolute() {
            continue;
        }
        let candidate = Path::new(dir).join(name);
        if let Ok(metadata) = fs::metadata(&candidate) {
            if metadata.permissions().mode() & 0o111 != 0 {
                return Some(candidate);
            }
        }
    }
    None
}

/// The paths a bare-name lookup would have tried, for the not-found detail.
fn executable_candidates(name: &Path, path_list: &str) -> Vec<String> {
    if name.is_absolute() {
        return vec![name.display().to_string()];
    }
    let name_str = name.to_string_lossy();
    if name_str.contains('/') {
        return match std::env::current_dir() {
            Ok(cwd) => vec![cwd.join(name).display().to_string()],
            Err(_) => vec![name_str.to_string()],
        };
    }
    let mut candidates = Vec::new();
    for dir in path_list.split(':') {
        if !dir.is_empty() && Path::new(dir).is_absolute() {
            candidates.push(Path::new(dir).join(name).display().to_string());
        }
    }
    if candidates.is_empty() {
        return vec![format!("{name_str} (PATH has no absolute entries)")];
    }
    candidates
}

fn pin_mismatch(detail: &str) -> ClientError {
    ClientError::new(REASON_PIN_MISMATCH, detail)
}

/// Re-proves the pinned executable immediately before a spawn (Go:
/// proveExecutable).
fn prove_executable(path: &Path, pin: &str) -> Result<(), ClientError> {
    let Some(expected) = hex_decode_32(pin) else {
        return Err(pin_mismatch("the pin is not 64 hex digits"));
    };
    if !path.is_absolute() {
        return Err(pin_mismatch("a pinned executable must be an absolute path"));
    }
    let resolved = fs::canonicalize(path).map_err(|_| pin_mismatch("executable is missing"))?;
    if resolved != path {
        return Err(pin_mismatch("executable path is not canonical"));
    }
    let mut file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)
        .map_err(|_| pin_mismatch("executable could not be opened without following links"))?;
    if !file
        .metadata()
        .map_err(|_| pin_mismatch("executable is not a regular file"))?
        .is_file()
    {
        return Err(pin_mismatch("executable is not a regular file"));
    }
    let mut hasher = Sha256::new();
    let mut total = 0u64;
    let mut chunk = [0u8; 65536];
    loop {
        match file.read(&mut chunk) {
            Ok(0) => break,
            Ok(n) => {
                total += n as u64;
                if total > MAX_EXECUTABLE {
                    return Err(pin_mismatch("executable exceeds the size limit"));
                }
                hasher.update(&chunk[..n]);
            }
            Err(ref error) if error.kind() == IoErrorKind::Interrupted => {}
            Err(_) => return Err(pin_mismatch("executable could not be read")),
        }
    }
    if !constant_time_eq(&hasher.finalize(), &expected) {
        return Err(pin_mismatch("executable digest does not match the pin"));
    }
    Ok(())
}

/// Decodes 64 hex digits into 32 bytes (Go: hex.DecodeString + size check).
fn hex_decode_32(pin: &str) -> Option<[u8; 32]> {
    let bytes = pin.as_bytes();
    if bytes.len() != 64 {
        return None;
    }
    let mut out = [0u8; 32];
    for (index, pair) in bytes.chunks_exact(2).enumerate() {
        out[index] = (hex_value(pair[0])? << 4) | hex_value(pair[1])?;
    }
    Some(out)
}

fn hex_value(byte: u8) -> Option<u8> {
    match byte {
        b'0'..=b'9' => Some(byte - b'0'),
        b'a'..=b'f' => Some(byte - b'a' + 10),
        b'A'..=b'F' => Some(byte - b'A' + 10),
        _ => None,
    }
}

/// Constant-time comparison (Go: crypto/subtle.ConstantTimeCompare).
fn constant_time_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    let mut diff = 0u8;
    for (left, right) in a.iter().zip(b) {
        diff |= left ^ right;
    }
    diff == 0
}

/// Returns the argv (after the executable) for one call. The workspace is not
/// sent: kei-proxy resolves identity and scope from the runtime token.
pub fn authorize_argv(req: &AuthorizeRequest) -> Vec<String> {
    let mut argv = vec![
        "authorize".to_string(),
        "--user".to_string(),
        req.user_id.clone(),
        "--tool".to_string(),
        req.tool.clone(),
        "--action".to_string(),
        req.action.clone(),
        "--resource".to_string(),
        req.resource.clone(),
    ];
    let depth = if req.delegation_depth > 0 {
        req.delegation_depth.to_string()
    } else {
        String::new()
    };
    let optional = [
        ("--span-id", req.span_id.clone()),
        ("--invoking-subject", req.invoking_subject.clone()),
        ("--parent-span", req.parent_span.clone()),
        ("--delegation-depth", depth),
        ("--agent-version", req.agent_version.clone()),
        ("--framework", req.framework.clone()),
        ("--tool-args-digest", req.tool_args_digest.clone()),
        ("--resources", req.resources.join(",")),
    ];
    for (flag, value) in optional {
        if !value.is_empty() {
            argv.push(flag.to_string());
            argv.push(value);
        }
    }
    argv
}

/// Validates one authorize result against kei-proxy's exit-code contract: 0
/// for allow/enrollment_required, 1 for deny, and 1 with empty stdout for
/// every error.
pub fn parse_authorize_output(stdout: &[u8], exit_code: i32) -> Result<Value, ClientError> {
    if exit_code != 0 && exit_code != 1 {
        return Err(ClientError::new(
            REASON_PROXY_ERROR,
            format!("kei-proxy exited {exit_code}"),
        ));
    }
    if String::from_utf8_lossy(stdout).trim().is_empty() {
        if exit_code != 0 {
            return Err(ClientError::new(
                REASON_PROXY_ERROR,
                format!("kei-proxy exited {exit_code}"),
            ));
        }
        return Err(ClientError::new(
            REASON_EMPTY_RESPONSE,
            "kei-proxy printed nothing",
        ));
    }
    let parsed: Value = match serde_json::from_slice(stdout) {
        Ok(parsed) => parsed,
        Err(_) => {
            return Err(ClientError::new(
                REASON_MALFORMED_RESPONSE,
                "stdout is not JSON",
            ));
        }
    };
    if !parsed.is_object() {
        return Err(ClientError::new(
            REASON_MALFORMED_RESPONSE,
            "stdout is not a JSON object",
        ));
    }
    if let Some(decision) = parsed.get("decision").and_then(Value::as_str) {
        let normalized = decision.trim().to_ascii_lowercase();
        if exit_code != 0 && is_affirmative(&normalized) {
            return Err(ClientError::new(
                REASON_EXIT_MISMATCH,
                format!("{decision} with exit {exit_code}"),
            ));
        }
    }
    Ok(parsed)
}

fn is_affirmative(decision: &str) -> bool {
    matches!(decision, "allow" | "permit")
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn request() -> AuthorizeRequest {
        AuthorizeRequest {
            user_id: "user-1".into(),
            tool: "github.get_issue".into(),
            action: "read".into(),
            resource: "github:repo:acme/widgets".into(),
            span_id: "span-1".into(),
            invoking_subject: "user-1".into(),
            parent_span: String::new(),
            delegation_depth: 2,
            agent_version: "1.0.0".into(),
            framework: "pi".into(),
            tool_args_digest: "digest".into(),
            resources: vec!["github:repo:acme/widgets".into()],
            workspace_id: "ws-1".into(),
        }
    }

    #[test]
    fn argv_carries_identity_as_flags_never_the_token() {
        let argv = authorize_argv(&request());
        assert_eq!(
            argv,
            [
                "authorize",
                "--user",
                "user-1",
                "--tool",
                "github.get_issue",
                "--action",
                "read",
                "--resource",
                "github:repo:acme/widgets",
                "--span-id",
                "span-1",
                "--invoking-subject",
                "user-1",
                "--delegation-depth",
                "2",
                "--agent-version",
                "1.0.0",
                "--framework",
                "pi",
                "--tool-args-digest",
                "digest",
                "--resources",
                "github:repo:acme/widgets",
            ]
        );
    }

    #[test]
    fn argv_omits_empty_optionals_and_zero_depth() {
        let mut req = request();
        req.span_id = String::new();
        req.parent_span = "parent".into();
        req.delegation_depth = 0;
        let argv = authorize_argv(&req);
        assert!(!argv.iter().any(|arg| arg == "--span-id"));
        assert!(!argv.iter().any(|arg| arg == "--delegation-depth"));
        assert!(argv.iter().any(|arg| arg == "--parent-span"));
    }

    #[test]
    fn child_env_draws_only_from_the_allowlist() {
        let client = CliClient {
            executable: PathBuf::new(),
            expected_sha256: None,
            timeout: Duration::ZERO,
            env: Some(HashMap::from([
                ("PATH".to_string(), "/usr/bin:/bin".to_string()),
                ("HOME".to_string(), "/home/user".to_string()),
                ("TZ".to_string(), "UTC".to_string()),
                (RUNTIME_TOKEN_ENV.to_string(), "token".to_string()),
                ("KEI_PROXY_AUDIT".to_string(), "on".to_string()),
                ("KEI_PROXY_IDENTITY".to_string(), "leaked".to_string()),
            ])),
        };
        assert_eq!(
            client.child_env(),
            HashMap::from([
                ("TZ".to_string(), "UTC".to_string()),
                (RUNTIME_TOKEN_ENV.to_string(), "token".to_string()),
                ("KEI_PROXY_AUDIT".to_string(), "on".to_string()),
            ])
        );
    }

    #[test]
    fn parse_follows_the_exit_code_contract() {
        let response =
            json!({"schema": "kei.authorize-response/v1", "decision": "deny", "reason": "no"});
        assert!(parse_authorize_output(response.to_string().as_bytes(), 0).is_ok());
        assert!(parse_authorize_output(response.to_string().as_bytes(), 1).is_ok());

        let error = parse_authorize_output(b"{}", 2).unwrap_err();
        assert_eq!(error.class, REASON_PROXY_ERROR);
        assert_eq!(error.detail, "kei-proxy exited 2");

        let error = parse_authorize_output(b"", 1).unwrap_err();
        assert_eq!(error.class, REASON_PROXY_ERROR);
        assert_eq!(error.detail, "kei-proxy exited 1");

        let error = parse_authorize_output(b"  \n", 0).unwrap_err();
        assert_eq!(error.class, REASON_EMPTY_RESPONSE);
        assert_eq!(error.detail, "kei-proxy printed nothing");

        let error = parse_authorize_output(b"not json", 0).unwrap_err();
        assert_eq!(error.class, REASON_MALFORMED_RESPONSE);
        assert_eq!(error.detail, "stdout is not JSON");

        let error = parse_authorize_output(b"[1]", 0).unwrap_err();
        assert_eq!(error.class, REASON_MALFORMED_RESPONSE);
        assert_eq!(error.detail, "stdout is not a JSON object");

        // An affirmative decision must not survive exit 1.
        let error = parse_authorize_output(br#"{"decision":"allow"}"#, 1).unwrap_err();
        assert_eq!(error.class, REASON_EXIT_MISMATCH);
        assert_eq!(error.detail, "allow with exit 1");
    }
}
