//! Backend construction (port of `go/llm/factory.go`).

use super::backend::Backend;
use super::server::{ServerBackend, ServerConfig};
use super::tokens::get_model_context_window;
use std::time::Duration;

/// Configuration for creating a backend (Go `Config`).
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Config {
    pub base_url: String,
    pub api_key: String,
    pub model: String,
    pub context_window: i64,
    pub timeout: Duration,
}

/// Creates a new backend based on the configuration. Currently supports
/// OpenAI-compatible servers. Go `NewBackend`: a zero timeout defaults to
/// 60s and a zero context window defaults to [`get_model_context_window`].
pub fn new_backend(config: Config) -> Result<Box<dyn Backend>, String> {
    let mut config = config;
    if config.timeout.is_zero() {
        config.timeout = Duration::from_secs(60);
    }
    if config.context_window == 0 {
        config.context_window = get_model_context_window(&config.model);
    }

    Ok(Box::new(ServerBackend::new(ServerConfig {
        base_url: config.base_url,
        api_key: config.api_key,
        model: config.model,
        context_window: config.context_window,
        timeout: config.timeout,
    })))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn new_backend_applies_defaults() {
        let backend = new_backend(Config {
            base_url: "http://127.0.0.1:1".into(),
            model: "gpt-4o".into(),
            ..Default::default()
        })
        .expect("backend");

        assert!(backend.supports_native_tool_calling());
        assert_eq!(backend.model_name(), "gpt-4o");
        assert_eq!(backend.context_window_size(), 128_000);
    }
}
