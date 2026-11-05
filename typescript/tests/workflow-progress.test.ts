import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { ProgressTracker } from "../src/middleware/guardrails/progress";

test("replays the shared progress fixture", () => {
  const data = JSON.parse(readFileSync(resolve(process.cwd(), "../fixtures/workflow/progress.v1.json"), "utf8"));
  const tracker = new ProgressTracker(data.required, data.max_no_progress);
  for (const event of data.events) {
    const result = event.kind === "failure" ? tracker.failure() : tracker.observe(event.key, event.present);
    expect(result).toEqual({ pending: event.pending, ready: event.ready, noProgress: event.no_progress, exhausted: event.exhausted });
  }
});

test("rejects invalid requirements and unknown facts", () => {
  expect(() => new ProgressTracker([])).toThrow();
  expect(() => new ProgressTracker(["a", "a"])).toThrow();
  expect(() => new ProgressTracker(["a"]).observe("b")).toThrow();
});
