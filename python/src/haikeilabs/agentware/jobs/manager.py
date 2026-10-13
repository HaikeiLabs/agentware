"""Job manager - Create and track async agent jobs."""

import threading
from datetime import datetime
from typing import Protocol

from .job import Job, JobStatus


class JobManager(Protocol):
    """Protocol for job managers."""

    def create(self, description: str) -> str:
        """Create a new job."""
        ...

    def start(self, job_id: str) -> None:
        """Mark a job as running."""
        ...

    def complete(self, job_id: str, result: str) -> None:
        """Mark a job as complete."""
        ...

    def fail(self, job_id: str, error: str) -> None:
        """Mark a job as failed."""
        ...

    def cancel(self, job_id: str) -> None:
        """Cancel a job."""
        ...

    def get(self, job_id: str) -> Job:
        """Get a job by ID."""
        ...

    def list(self, status: JobStatus | None = None) -> list[Job]:
        """List jobs, optionally filtered by status."""
        ...


class InMemoryJobManager:
    """In-memory implementation of JobManager."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.RLock()
        self._counter = 0

    def create(self, description: str) -> str:
        """Create a new job."""
        with self._lock:
            self._counter += 1
            job_id = f"{datetime.now().strftime('%Y%m%d%H%M%S')}-{self._counter:04d}"
            now = datetime.now()
            job = Job(
                id=job_id,
                status=JobStatus.PENDING,
                description=description,
                created_at=now,
                updated_at=now,
            )
            self._jobs[job_id] = job
            return job_id

    def start(self, job_id: str) -> None:
        """Mark a job as running."""
        with self._lock:
            if job_id not in self._jobs:
                raise KeyError(f"job not found: {job_id}")
            job = self._jobs[job_id]
            job.status = JobStatus.RUNNING
            job.updated_at = datetime.now()

    def complete(self, job_id: str, result: str) -> None:
        """Mark a job as complete."""
        with self._lock:
            if job_id not in self._jobs:
                raise KeyError(f"job not found: {job_id}")
            job = self._jobs[job_id]
            job.status = JobStatus.COMPLETE
            job.result = result
            job.updated_at = datetime.now()

    def fail(self, job_id: str, error: str) -> None:
        """Mark a job as failed."""
        with self._lock:
            if job_id not in self._jobs:
                raise KeyError(f"job not found: {job_id}")
            job = self._jobs[job_id]
            job.status = JobStatus.FAILED
            job.error = error
            job.updated_at = datetime.now()

    def cancel(self, job_id: str) -> None:
        """Cancel a job."""
        with self._lock:
            if job_id not in self._jobs:
                raise KeyError(f"job not found: {job_id}")
            job = self._jobs[job_id]
            job.status = JobStatus.CANCELED
            job.updated_at = datetime.now()

    def get(self, job_id: str) -> Job:
        """Get a job by ID."""
        with self._lock:
            if job_id not in self._jobs:
                raise KeyError(f"job not found: {job_id}")
            return self._jobs[job_id]

    def list(self, status: JobStatus | None = None) -> list[Job]:
        """List jobs, optionally filtered by status."""
        with self._lock:
            if status is None:
                return list(self._jobs.values())
            return [j for j in self._jobs.values() if j.status == status]
