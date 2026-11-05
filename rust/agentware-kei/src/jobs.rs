//! In-memory async agent job manager, ported from `go/jobs`
//! (`job.go`, `manager.go`, `errors.go`).

use std::collections::HashMap;
use std::fmt;
use std::sync::RwLock;
use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::mpsc::{Receiver, SyncSender, sync_channel};
use std::time::{SystemTime, UNIX_EPOCH};

/// The current state of an async agent job (go/jobs `Status`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum JobStatus {
    /// The job has been created but not started (`StatusPending`).
    Pending,
    /// The job is currently executing (`StatusRunning`).
    Running,
    /// The job finished successfully (`StatusComplete`).
    Complete,
    /// The job finished with an error (`StatusFailed`).
    Failed,
    /// The job was cancelled before completion (`StatusCanceled`).
    Canceled,
}

impl JobStatus {
    /// The Go string value of the status.
    pub fn as_str(self) -> &'static str {
        match self {
            JobStatus::Pending => "pending",
            JobStatus::Running => "running",
            JobStatus::Complete => "complete",
            JobStatus::Failed => "failed",
            JobStatus::Canceled => "canceled",
        }
    }
}

impl fmt::Display for JobStatus {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// A single async agent task (go/jobs `Job`).
#[derive(Debug, Clone, PartialEq)]
pub struct Job {
    pub id: String,
    pub status: JobStatus,
    pub description: String,
    pub created_at: SystemTime,
    pub updated_at: SystemTime,
    pub result: String,
    pub error: String,
}

/// Errors returned by job operations (go/jobs `errors.go`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum JobError {
    /// Returned when a job with the given ID does not exist
    /// (go/jobs `ErrJobNotFound`).
    JobNotFound,
}

impl fmt::Display for JobError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            JobError::JobNotFound => f.write_str("jobs: job not found"),
        }
    }
}

impl std::error::Error for JobError {}

/// Creates and tracks async agent jobs (go/jobs `JobManager`).
pub trait JobManager {
    /// Registers a new job and returns its ID.
    fn create(&self, description: &str) -> Result<String, JobError>;
    /// Marks a job as running. Should be called immediately before execution.
    fn start(&self, job_id: &str) -> Result<(), JobError>;
    /// Marks a job as done with a result.
    fn complete(&self, job_id: &str, result: &str) -> Result<(), JobError>;
    /// Marks a job as failed with an error message.
    fn fail(&self, job_id: &str, err_msg: &str) -> Result<(), JobError>;
    /// Marks a job as canceled.
    fn cancel(&self, job_id: &str) -> Result<(), JobError>;
    /// Retrieves the current state of a job.
    fn get(&self, job_id: &str) -> Result<Job, JobError>;
    /// Returns all jobs, optionally filtered by status.
    fn list(&self, status: Option<JobStatus>) -> Vec<Job>;
    /// Returns a receiver of job state updates, seeded with the current
    /// state. Drop the receiver to stop watching.
    fn watch(&self, job_id: &str) -> Result<Receiver<Job>, JobError>;
}

/// An in-memory implementation of JobManager (go/jobs
/// `inMemoryJobManager`).
#[derive(Default)]
pub struct InMemoryJobManager {
    inner: RwLock<Inner>,
}

#[derive(Default)]
struct Inner {
    jobs: HashMap<String, Job>,
    chans: HashMap<String, SyncSender<Job>>,
}

impl InMemoryJobManager {
    /// Creates a new in-memory JobManager (go/jobs `NewInMemoryManager`).
    pub fn new() -> Self {
        Self::default()
    }
}

impl JobManager for InMemoryJobManager {
    fn create(&self, description: &str) -> Result<String, JobError> {
        let mut inner = self.inner.write().unwrap();
        let id = generate_job_id();
        let now = SystemTime::now();
        inner.jobs.insert(
            id.clone(),
            Job {
                id: id.clone(),
                status: JobStatus::Pending,
                description: description.to_string(),
                created_at: now,
                updated_at: now,
                result: String::new(),
                error: String::new(),
            },
        );
        Ok(id)
    }

    fn start(&self, job_id: &str) -> Result<(), JobError> {
        let mut inner = self.inner.write().unwrap();
        let job = inner.jobs.get_mut(job_id).ok_or(JobError::JobNotFound)?;
        job.status = JobStatus::Running;
        job.updated_at = SystemTime::now();
        let snapshot = job.clone();
        notify(&inner, &snapshot);
        Ok(())
    }

    fn complete(&self, job_id: &str, result: &str) -> Result<(), JobError> {
        let mut inner = self.inner.write().unwrap();
        let job = inner.jobs.get_mut(job_id).ok_or(JobError::JobNotFound)?;
        job.status = JobStatus::Complete;
        job.result = result.to_string();
        job.updated_at = SystemTime::now();
        let snapshot = job.clone();
        notify(&inner, &snapshot);
        Ok(())
    }

    fn fail(&self, job_id: &str, err_msg: &str) -> Result<(), JobError> {
        let mut inner = self.inner.write().unwrap();
        let job = inner.jobs.get_mut(job_id).ok_or(JobError::JobNotFound)?;
        job.status = JobStatus::Failed;
        job.error = err_msg.to_string();
        job.updated_at = SystemTime::now();
        let snapshot = job.clone();
        notify(&inner, &snapshot);
        Ok(())
    }

    fn cancel(&self, job_id: &str) -> Result<(), JobError> {
        let mut inner = self.inner.write().unwrap();
        let job = inner.jobs.get_mut(job_id).ok_or(JobError::JobNotFound)?;
        job.status = JobStatus::Canceled;
        job.updated_at = SystemTime::now();
        let snapshot = job.clone();
        notify(&inner, &snapshot);
        Ok(())
    }

    fn get(&self, job_id: &str) -> Result<Job, JobError> {
        let inner = self.inner.read().unwrap();
        inner.jobs.get(job_id).cloned().ok_or(JobError::JobNotFound)
    }

    fn list(&self, status: Option<JobStatus>) -> Vec<Job> {
        let inner = self.inner.read().unwrap();
        inner
            .jobs
            .values()
            .filter(|job| status.is_none_or(|s| job.status == s))
            .cloned()
            .collect()
    }

    fn watch(&self, job_id: &str) -> Result<Receiver<Job>, JobError> {
        let mut inner = self.inner.write().unwrap();
        let job = inner.jobs.get(job_id).ok_or(JobError::JobNotFound)?;
        let (tx, rx) = sync_channel(1);
        tx.send(job.clone())
            .expect("buffered channel: receiver is alive");
        inner.chans.insert(job_id.to_string(), tx);
        Ok(rx)
    }
}

/// Sends a state update to the watcher of `job`, if any, dropping it when
/// the watcher's buffer is full (go/jobs `notify`).
fn notify(inner: &Inner, job: &Job) {
    if let Some(ch) = inner.chans.get(&job.id) {
        let _ = ch.try_send(job.clone());
    }
}

static JOB_ID_COUNTER: AtomicI64 = AtomicI64::new(0);

/// Generates a job ID: wall-clock `YYYYMMDDHHMMSS` plus a zero-padded
/// sequence number (go/jobs `generateJobID`). The Go port formats the local
/// wall clock; this port has no local-time dependency and formats UTC.
fn generate_job_id() -> String {
    let id = JOB_ID_COUNTER.fetch_add(1, Ordering::SeqCst) + 1;
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let (year, month, day, hour, minute, second) = civil((secs / 86_400) as i64, secs % 86_400);
    format!(
        "{year:04}{month:02}{day:02}{hour:02}{minute:02}{second:02}-{:04}",
        id % 10_000
    )
}

/// Converts days since the Unix epoch and seconds within the day to a civil
/// UTC date and time (Howard Hinnant's `civil_from_days`).
fn civil(days: i64, rem: u64) -> (i64, u32, u32, u32, u32, u32) {
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097; // [0, 146096]
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365; // [0, 399]
    let year = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0, 365]
    let mp = (5 * doy + 2) / 153; // [0, 11]
    let day = (doy - (153 * mp + 2) / 5 + 1) as u32; // [1, 31]
    let month = if mp < 10 { mp + 3 } else { mp - 9 }; // [1, 12]
    let year = if month <= 2 { year + 1 } else { year };
    (
        year,
        month as u32,
        day,
        (rem / 3600) as u32,
        ((rem % 3600) / 60) as u32,
        (rem % 60) as u32,
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    // Mirrors go/jobs/job_test.go TestStatusConstants.
    #[test]
    fn status_constants() {
        assert_eq!(JobStatus::Pending.as_str(), "pending");
        assert_eq!(JobStatus::Running.as_str(), "running");
        assert_eq!(JobStatus::Complete.as_str(), "complete");
        assert_eq!(JobStatus::Failed.as_str(), "failed");
        assert_eq!(JobStatus::Canceled.as_str(), "canceled");
    }

    // Mirrors go/jobs/job_test.go TestJob.
    #[test]
    fn job_fields() {
        let now = SystemTime::now();
        let job = Job {
            id: "job_123".to_string(),
            status: JobStatus::Running,
            description: "Process data".to_string(),
            created_at: now,
            updated_at: now,
            result: String::new(),
            error: String::new(),
        };

        assert_eq!(job.id, "job_123");
        assert_eq!(job.status, JobStatus::Running);
        assert_eq!(job.description, "Process data");
    }

    // Mirrors go/jobs/job_test.go TestJobResultAndError.
    #[test]
    fn job_result_and_error() {
        let job = Job {
            id: "job_456".to_string(),
            status: JobStatus::Complete,
            description: String::new(),
            created_at: SystemTime::now(),
            updated_at: SystemTime::now(),
            result: "processed 100 items".to_string(),
            error: String::new(),
        };
        assert_eq!(job.result, "processed 100 items");

        let job2 = Job {
            id: "job_789".to_string(),
            status: JobStatus::Failed,
            description: String::new(),
            created_at: SystemTime::now(),
            updated_at: SystemTime::now(),
            result: String::new(),
            error: "connection timeout".to_string(),
        };
        assert_eq!(job2.error, "connection timeout");
    }

    // Mirrors go/jobs/manager_test.go TestNewInMemoryManager.
    #[test]
    fn new_in_memory_manager() {
        let mgr = InMemoryJobManager::new();
        assert!(mgr.list(None).is_empty());
    }

    // Mirrors go/jobs/manager_test.go TestJobManagerCreate.
    #[test]
    fn create() {
        let mgr = InMemoryJobManager::new();

        let job_id = mgr.create("test job").expect("create succeeds");
        assert!(!job_id.is_empty());

        let job = mgr.get(&job_id).expect("get succeeds");
        assert_eq!(job.status, JobStatus::Pending);
        assert_eq!(job.description, "test job");
    }

    // Mirrors go/jobs/manager_test.go TestJobManagerStart.
    #[test]
    fn start() {
        let mgr = InMemoryJobManager::new();

        let job_id = mgr.create("test job").expect("create succeeds");
        mgr.start(&job_id).expect("start succeeds");

        let job = mgr.get(&job_id).expect("get succeeds");
        assert_eq!(job.status, JobStatus::Running);
    }

    // Mirrors go/jobs/manager_test.go TestJobManagerComplete.
    #[test]
    fn complete() {
        let mgr = InMemoryJobManager::new();

        let job_id = mgr.create("test job").expect("create succeeds");
        mgr.start(&job_id).expect("start succeeds");
        mgr.complete(&job_id, "result data")
            .expect("complete succeeds");

        let job = mgr.get(&job_id).expect("get succeeds");
        assert_eq!(job.status, JobStatus::Complete);
        assert_eq!(job.result, "result data");
    }

    // Mirrors go/jobs/manager_test.go TestJobManagerFail.
    #[test]
    fn fail() {
        let mgr = InMemoryJobManager::new();

        let job_id = mgr.create("test job").expect("create succeeds");
        mgr.start(&job_id).expect("start succeeds");
        mgr.fail(&job_id, "something went wrong")
            .expect("fail succeeds");

        let job = mgr.get(&job_id).expect("get succeeds");
        assert_eq!(job.status, JobStatus::Failed);
        assert_eq!(job.error, "something went wrong");
    }

    // Mirrors go/jobs/manager_test.go TestJobManagerCancel.
    #[test]
    fn cancel() {
        let mgr = InMemoryJobManager::new();

        let job_id = mgr.create("test job").expect("create succeeds");
        mgr.cancel(&job_id).expect("cancel succeeds");

        let job = mgr.get(&job_id).expect("get succeeds");
        assert_eq!(job.status, JobStatus::Canceled);
    }

    // Mirrors go/jobs/manager_test.go TestJobManagerGet_NotFound.
    #[test]
    fn get_not_found() {
        let mgr = InMemoryJobManager::new();

        let err = mgr.get("nonexistent").unwrap_err();
        assert_eq!(err, JobError::JobNotFound);
        assert_eq!(err.to_string(), "jobs: job not found");
    }

    // Mirrors go/jobs/manager_test.go TestJobManagerList.
    #[test]
    fn list() {
        let mgr = InMemoryJobManager::new();

        mgr.create("job1").expect("create succeeds");
        mgr.create("job2").expect("create succeeds");
        mgr.create("job3").expect("create succeeds");

        assert_eq!(mgr.list(None).len(), 3);

        assert_eq!(mgr.list(Some(JobStatus::Pending)).len(), 3);
    }

    // Mirrors go/jobs/manager_test.go TestJobManagerList_FilterByStatus.
    #[test]
    fn list_filter_by_status() {
        let mgr = InMemoryJobManager::new();

        let id1 = mgr.create("job1").expect("create succeeds");
        let id2 = mgr.create("job2").expect("create succeeds");

        mgr.start(&id1).expect("start succeeds");
        mgr.start(&id2).expect("start succeeds");

        assert_eq!(mgr.list(Some(JobStatus::Running)).len(), 2);
        assert_eq!(mgr.list(Some(JobStatus::Pending)).len(), 0);
    }

    // Mirrors go/jobs/manager_test.go TestJobManagerWatch.
    #[test]
    fn watch() {
        let mgr = InMemoryJobManager::new();

        let job_id = mgr.create("test job").expect("create succeeds");

        let rx = mgr.watch(&job_id).expect("watch succeeds");

        let job = rx
            .recv_timeout(Duration::from_secs(1))
            .expect("seeded state arrives");
        assert_eq!(job.id, job_id);
    }

    // Mirrors go/jobs/manager_test.go TestJobManagerWatch_NotFound.
    #[test]
    fn watch_not_found() {
        let mgr = InMemoryJobManager::new();

        let err = mgr.watch("nonexistent").unwrap_err();
        assert_eq!(err, JobError::JobNotFound);
    }

    // Covers go/jobs `notify`: a watcher receives each state update, and a
    // full watcher buffer drops the update instead of blocking the manager.
    #[test]
    fn watch_receives_updates() {
        let mgr = InMemoryJobManager::new();

        let job_id = mgr.create("test job").expect("create succeeds");
        let rx = mgr.watch(&job_id).expect("watch succeeds");
        assert_eq!(
            rx.recv_timeout(Duration::from_secs(1))
                .expect("seeded state arrives")
                .status,
            JobStatus::Pending
        );

        mgr.start(&job_id).expect("start succeeds");
        assert_eq!(
            rx.recv_timeout(Duration::from_secs(1))
                .expect("start update arrives")
                .status,
            JobStatus::Running
        );

        mgr.complete(&job_id, "done").expect("complete succeeds");

        // The buffer now holds the Complete update, so the next two updates
        // are dropped instead of blocking the manager.
        mgr.fail(&job_id, "boom").expect("fail succeeds");
        mgr.cancel(&job_id).expect("cancel succeeds");
        assert_eq!(
            rx.recv_timeout(Duration::from_secs(1))
                .expect("the buffered update survives full-buffer drops")
                .status,
            JobStatus::Complete
        );
        assert!(rx.recv_timeout(Duration::from_millis(50)).is_err());
    }

    // Covers go/jobs `generateJobID`: the sequence number is unique per
    // process and the ID carries the YYYYMMDDHHMMSS prefix.
    #[test]
    fn generated_ids_are_unique_and_formatted() {
        let mgr = InMemoryJobManager::new();

        let a = mgr.create("a").expect("create succeeds");
        let b = mgr.create("b").expect("create succeeds");
        assert_ne!(a, b);
        assert_eq!(a.len(), 14 + 1 + 4); // YYYYMMDDHHMMSS-NNNN
        assert!(a.chars().take(14).all(|c| c.is_ascii_digit()));
        assert_eq!(a.as_bytes()[14], b'-');
    }
}
