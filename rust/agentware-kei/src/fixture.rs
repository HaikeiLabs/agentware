//! Loader for the shared cross-language JSON contract fixtures.

use std::path::{Path, PathBuf};

/// Loads a contract fixture, where `relative_path` is relative to
/// `testing/contracts/` at the repository root.
pub fn load_contract(relative_path: impl AsRef<Path>) -> Result<serde_json::Value, String> {
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let repo_root = manifest_dir
        .parent()
        .and_then(Path::parent)
        .ok_or_else(|| "could not determine repository root from CARGO_MANIFEST_DIR".to_owned())?;
    let fixture_path = repo_root.join("testing/contracts").join(relative_path);
    let contents = std::fs::read_to_string(&fixture_path)
        .map_err(|error| format!("read {}: {error}", fixture_path.display()))?;
    serde_json::from_str(&contents)
        .map_err(|error| format!("parse {}: {error}", fixture_path.display()))
}
