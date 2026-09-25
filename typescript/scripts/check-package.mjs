#!/usr/bin/env node
// Release-readiness check for @haikeilabs/agentware.
//
// Packs the package exactly as `npm publish` would (prepack runs the build),
// asserts the tarball carries the public entry point and the RuntimeLink
// contract artifacts, then installs the tarball into a clean consumer project
// and imports and type-checks RuntimeLink from the package root.
//
// Usage: node scripts/check-package.mjs [--no-install]
//   --no-install  only check the tarball file list (no registry access).
import { execFileSync } from "node:child_process";
import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const pkgDir = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const pkg = JSON.parse(readFileSync(join(pkgDir, "package.json"), "utf8"));
const install = !process.argv.includes("--no-install");

// Files a consumer needs to import the RuntimeLink contract from the root.
const REQUIRED_FILES = [
  "package.json",
  pkg.main,
  pkg.types,
  "dist/kei/index.js",
  "dist/kei/index.d.ts",
  "dist/kei/runtimeLink.js",
  "dist/kei/runtimeLink.d.ts",
  "dist/kei/runtimeLinkRuntime.js",
  "dist/kei/runtimeLinkRuntime.d.ts",
];
// Nothing outside the build output may ship.
const FORBIDDEN_PREFIXES = ["src/", "tests/", "node_modules/", "scripts/"];

// Runtime and type-level surface a consumer imports from the package root.
const RUNTIME_EXPORTS = [
  "RUNTIME_LINK_CONTRACT_VERSION",
  "HARNESS_KINDS",
  "LINK_STATES",
  "FAILURE_CLASSES",
  "BEAT_OUTCOMES",
  "LIFECYCLE_EVENT_NAMES",
  "CHILD_ENV_ALLOWLIST",
  "RuntimeLinkConfigError",
  "ChildLineError",
  "InvalidLinkEventError",
  "defaultRuntimeLinkConfig",
  "normalizeRuntimeLinkConfig",
  "runtimeLinkConfigFromEnv",
  "backoffDelayMs",
  "waitBackoff",
  "parseChildLine",
  "linkEventFromWire",
  "linkEventToWire",
  "newRuntimeLink",
];

function run(cmd, args, cwd) {
  return execFileSync(cmd, args, {
    cwd,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "inherit"],
  });
}

function fail(message) {
  console.error(`check-package: FAIL: ${message}`);
  process.exitCode = 1;
}

const work = mkdtempSync(join(tmpdir(), "agentware-pack-"));
try {
  const [packed] = JSON.parse(
    run("npm", ["pack", "--json", "--pack-destination", work], pkgDir),
  );
  const files = new Set(packed.files.map((f) => f.path));
  console.log(
    `check-package: packed ${packed.name}@${packed.version} (${files.size} files)`,
  );

  for (const f of REQUIRED_FILES)
    if (!files.has(f)) fail(`tarball is missing ${f}`);
  for (const f of files) {
    if (FORBIDDEN_PREFIXES.some((p) => f.startsWith(p)))
      fail(`tarball must not ship ${f}`);
  }
  if (process.exitCode) process.exit();
  if (!install) {
    console.log("check-package: file list OK (install skipped)");
    process.exit();
  }

  const consumer = join(work, "consumer");
  mkdirSync(consumer);
  writeFileSync(
    join(consumer, "package.json"),
    JSON.stringify({
      name: "agentware-consumer",
      private: true,
      type: "module",
    }),
  );
  run(
    "npm",
    [
      "install",
      join(work, packed.filename),
      "--no-audit",
      "--no-fund",
      "--no-package-lock",
      "--ignore-scripts",
    ],
    consumer,
  );

  writeFileSync(
    join(consumer, "runtime.mjs"),
    `import * as aw from ${JSON.stringify(pkg.name)};
const missing = ${JSON.stringify(RUNTIME_EXPORTS)}.filter((n) => aw[n] === undefined);
if (missing.length) throw new Error("missing root exports: " + missing.join(", "));
const ev = aw.parseChildLine('{"v":1,"event":"terminal","run_id":"r1","reason":"unauthorized"}');
if (ev.kind !== "terminal") throw new Error("parseChildLine returned " + ev.kind);
const cfg = aw.runtimeLinkConfigFromEnv({});
if (cfg.enabled !== false) throw new Error("empty env must be disabled (local-only mode)");
console.log("runtime import OK, contract v" + aw.RUNTIME_LINK_CONTRACT_VERSION);
`,
  );
  process.stdout.write(
    `check-package: ${run("node", ["runtime.mjs"], consumer)}`,
  );

  writeFileSync(
    join(consumer, "types.ts"),
    `import { newRuntimeLink, parseChildLine, runtimeLinkConfigFromEnv } from ${JSON.stringify(pkg.name)};
import type { ChildEvent, LinkEvent, LinkStatus, RuntimeLink, RuntimeLinkConfig, RuntimeChildFactory } from ${JSON.stringify(pkg.name)};
const cfg: RuntimeLinkConfig = runtimeLinkConfigFromEnv({});
const ev: ChildEvent = parseChildLine("{}");
declare const factory: RuntimeChildFactory;
export type Surface = [typeof cfg, typeof ev, LinkEvent, LinkStatus, RuntimeLink, typeof newRuntimeLink, typeof factory];
`,
  );
  run(
    process.execPath,
    [
      join(pkgDir, "node_modules", "typescript", "bin", "tsc"),
      "--noEmit",
      "--strict",
      "--module",
      "nodenext",
      "--moduleResolution",
      "nodenext",
      "--target",
      "es2022",
      "types.ts",
    ],
    consumer,
  );
  console.log("check-package: type import OK (nodenext)");
} finally {
  rmSync(work, { recursive: true, force: true });
}
