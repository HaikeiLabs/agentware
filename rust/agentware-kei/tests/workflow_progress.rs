use agentware_kei::progress::ProgressTracker;
use serde_json::Value;

#[test]
fn replays_shared_fixture() {
    let data: Value =
        serde_json::from_str(include_str!("../../../fixtures/workflow/progress.v1.json")).unwrap();
    let required = data["required"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap().to_owned())
        .collect();
    let mut tracker =
        ProgressTracker::new(required, data["max_no_progress"].as_u64().unwrap() as usize).unwrap();
    for event in data["events"].as_array().unwrap() {
        let result = if event["kind"] == "failure" {
            tracker.failure()
        } else {
            tracker
                .observe(
                    event["key"].as_str().unwrap(),
                    event["present"].as_bool().unwrap(),
                )
                .unwrap()
        };
        let pending: Vec<String> = event["pending"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap().to_owned())
            .collect();
        assert_eq!(result.pending, pending);
        assert_eq!(result.ready, event["ready"].as_bool().unwrap());
        assert_eq!(
            result.no_progress,
            event["no_progress"].as_u64().unwrap() as usize
        );
        assert_eq!(result.exhausted, event["exhausted"].as_bool().unwrap());
    }
}

#[test]
fn rejects_invalid_requirements_and_unknown_fact() {
    assert!(ProgressTracker::new(vec![], 2).is_err());
    assert!(ProgressTracker::new(vec!["a".into(), "a".into()], 2).is_err());
    let mut tracker = ProgressTracker::new(vec!["a".into()], 2).unwrap();
    assert!(tracker.observe("b", true).is_err());
}
