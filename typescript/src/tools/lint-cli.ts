#!/usr/bin/env node
import { readFileSync } from "node:fs";
import { lintKeiToolManifest } from "./lint.js";

const file = process.argv[2];
if (!file) {
  console.log(
    JSON.stringify({
      valid: false,
      errors: ["usage: agentware-lint-tools MANIFEST.json"],
    }),
  );
  process.exitCode = 1;
} else {
  try {
    const errors = lintKeiToolManifest(readFileSync(file, "utf8"));
    console.log(JSON.stringify({ valid: errors.length === 0, errors }));
    if (errors.length) process.exitCode = 1;
  } catch (error) {
    console.log(
      JSON.stringify({
        valid: false,
        errors: [error instanceof Error ? error.message : "lint failed"],
      }),
    );
    process.exitCode = 1;
  }
}
