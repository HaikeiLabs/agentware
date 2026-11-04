//! Per-key sliding-window rate limiting (port of `go/middleware/ratelimit.go`).
//!
//! Each key counts calls in a window that starts at the first call; a key
//! without a configured window uses the one-minute default. The limiter is
//! safe for shared use across threads, mirroring the Go mutex.

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{Duration, SystemTime};

#[derive(Debug)]
struct RateCounter {
    count: i32,
    first_call: SystemTime,
}

#[derive(Debug, Default)]
struct RateLimiterInner {
    counters: HashMap<String, RateCounter>,
    windows: HashMap<String, Duration>,
}

/// A per-key rate limiter (Go `RateLimiter`).
#[derive(Debug, Default)]
pub struct RateLimiter {
    inner: Mutex<RateLimiterInner>,
}

impl RateLimiter {
    /// Creates a limiter with no configured windows (Go `NewRateLimiter`).
    pub fn new() -> Self {
        Self::default()
    }

    /// Sets the window for a key (Go `SetWindow`).
    pub fn set_window(&self, key: &str, window: Duration) {
        self.inner
            .lock()
            .expect("rate limiter mutex poisoned")
            .windows
            .insert(key.to_owned(), window);
    }

    /// Whether a call for a key is within its limit (Go `Allow`). A key
    /// without a configured window uses the one-minute default; the window
    /// restarts at the first call of a new window.
    pub fn allow(&self, key: &str, limit: i32) -> bool {
        let mut inner = self.inner.lock().expect("rate limiter mutex poisoned");

        let window = inner.windows.get(key).copied().unwrap_or_default();
        let window = if window.is_zero() {
            Duration::from_secs(60)
        } else {
            window
        };

        let now = SystemTime::now();
        match inner.counters.get_mut(key) {
            None => {
                inner.counters.insert(
                    key.to_owned(),
                    RateCounter {
                        count: 1,
                        first_call: now,
                    },
                );
                true
            }
            Some(counter)
                if now.duration_since(counter.first_call).unwrap_or_default() > window =>
            {
                inner.counters.insert(
                    key.to_owned(),
                    RateCounter {
                        count: 1,
                        first_call: now,
                    },
                );
                true
            }
            Some(counter) => {
                if counter.count >= limit {
                    return false;
                }
                counter.count += 1;
                true
            }
        }
    }

    /// Drops the counter for a key (Go `Reset`).
    pub fn reset(&self, key: &str) {
        self.inner
            .lock()
            .expect("rate limiter mutex poisoned")
            .counters
            .remove(key);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Go has no rate limiter tests; these pin the ported behavior.

    #[test]
    fn first_call_is_allowed() {
        let limiter = RateLimiter::new();
        assert!(limiter.allow("key", 1));
    }

    #[test]
    fn limit_is_enforced_within_the_window() {
        let limiter = RateLimiter::new();
        limiter.set_window("key", Duration::from_secs(60));
        assert!(limiter.allow("key", 2));
        assert!(limiter.allow("key", 2));
        assert!(!limiter.allow("key", 2));
        assert!(!limiter.allow("key", 2));
    }

    #[test]
    fn keys_are_independent() {
        let limiter = RateLimiter::new();
        assert!(limiter.allow("a", 1));
        assert!(!limiter.allow("a", 1));
        assert!(limiter.allow("b", 1));
    }

    #[test]
    fn the_window_restarts_after_expiry() {
        let limiter = RateLimiter::new();
        limiter.set_window("key", Duration::from_millis(50));
        assert!(limiter.allow("key", 1));
        assert!(!limiter.allow("key", 1));
        std::thread::sleep(Duration::from_millis(150));
        assert!(limiter.allow("key", 1));
    }

    #[test]
    fn reset_allows_again() {
        let limiter = RateLimiter::new();
        limiter.set_window("key", Duration::from_secs(60));
        assert!(limiter.allow("key", 1));
        assert!(!limiter.allow("key", 1));
        limiter.reset("key");
        assert!(limiter.allow("key", 1));
    }

    #[test]
    fn the_default_window_applies_without_set_window() {
        // No SetWindow: the one-minute default window is in effect, so the
        // limit holds across immediate calls.
        let limiter = RateLimiter::new();
        assert!(limiter.allow("key", 2));
        assert!(limiter.allow("key", 2));
        assert!(!limiter.allow("key", 2));
    }
}
