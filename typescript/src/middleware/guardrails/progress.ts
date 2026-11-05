/** A progress observation is supplied by the application, not the model. */
export interface ProgressSnapshot {
  pending: string[];
  ready: boolean;
  noProgress: number;
  exhausted: boolean;
}

/** Tracks verified facts for one workflow; it does not authorize tool calls. */
export class ProgressTracker {
  private readonly required: string[];
  private readonly satisfied = new Set<string>();
  private readonly maxNoProgress: number;
  private failures = 0;

  constructor(required: string[], maxNoProgress = 2) {
    if (!required.length || required.some((key) => !key.trim()) || new Set(required).size !== required.length || !Number.isInteger(maxNoProgress) || maxNoProgress < 0) {
      throw new Error("invalid progress configuration");
    }
    this.required = [...required];
    this.maxNoProgress = maxNoProgress;
  }

  /** A new verified fact resets the no-progress counter; invalidation does not. */
  observe(key: string, present = true): ProgressSnapshot {
    if (!this.required.includes(key)) throw new Error("unknown requirement");
    if (present && !this.satisfied.has(key)) { this.satisfied.add(key); this.failures = 0; }
    else if (!present) this.satisfied.delete(key);
    return this.snapshot();
  }

  /** Count an unsuccessful attempt; configure a separate total budget as well. */
  failure(): ProgressSnapshot { this.failures++; return this.snapshot(); }

  /** Return a fresh snapshot without mutating state. */
  snapshot(): ProgressSnapshot {
    const pending = this.required.filter((key) => !this.satisfied.has(key));
    return { pending, ready: pending.length === 0, noProgress: this.failures, exhausted: this.failures > this.maxNoProgress };
  }
}
