/**
 * KeiProxyEvaluator parity table (fixtures/kei/authorize-cases.v1.json).
 *
 * Every case runs the shared fake kei-proxy (fixtures/kei/fake-kei-proxy.sh)
 * as a real subprocess. Python and Go load the same files and must reach the
 * same decision for every case.
 */
import { createHash } from "node:crypto";
import {
  chmodSync,
  copyFileSync,
  existsSync,
  mkdtempSync,
  readFileSync,
  realpathSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import {
  Action,
  AUTHORIZE_CHILD_ENV_ALLOWLIST,
  KEI_PROXY_REASON_CLASSES,
  KeiProxyAuthorizeClient,
  KeiProxyAuthorizeError,
  KeiProxyEvaluator,
  keiProxyResourcesTouched,
  keiProxyToolArgsDigest,
} from "../src/index.js";
import type { CallerContext, Decision, KeiProxyAuthorizeRequest } from "../src/index.js";

type Json = Record<string, any>;

const FIXTURES = resolve(process.cwd(), "..", "fixtures", "kei");
const load = (name: string): Json =>
  JSON.parse(readFileSync(join(FIXTURES, name), "utf8"));
const TABLE = load("authorize-cases.v1.json");
const POLICY = load(TABLE.policy);
const CASES: Json[] = TABLE.cases;
const CANARIES: Record<string, string> = TABLE.canaries;

function buildCaller(c: Json): CallerContext {
  const subject = POLICY.users[c.input.user].id as string;
  const overrides = c.input.caller ?? {};
  return {
    user_id: overrides.user_id ?? subject,
    invoking_subject: subject,
    parent_span: overrides.parent_span ?? "",
    delegation_depth: overrides.delegation_depth ?? 0,
    trusted: false,
    metadata: {
      span_id: TABLE.caller_defaults.span_id,
      agent_id: POLICY.agent.id,
      agent_version: POLICY.agent.version,
      framework: POLICY.agent.framework,
      workspace_id: POLICY.workspace.id,
    },
  };
}

function buildEnv(c: Json): Record<string, string> {
  const env: Record<string, string> = { PATH: "/usr/bin:/bin" };
  for (const name of TABLE.child_env.stripped as string[]) env[name] = `leaked-${name}`;
  if (!c.env?.omit_token) env.KEI_RUNTIME_TOKEN = CANARIES.runtime_token;
  return env;
}

function installFake(c: Json, dir: string): string {
  const fake = join(dir, "kei-proxy");
  if (c.proxy.missing_binary) return fake;
  const real = c.proxy.symlink ? join(dir, "kei-proxy-real") : fake;
  copyFileSync(join(FIXTURES, "fake-kei-proxy.sh"), real);
  chmodSync(real, 0o755);
  if (c.proxy.symlink) symlinkSync(real, fake);
  writeFileSync(join(dir, "stdout"), c.proxy.stdout);
  writeFileSync(join(dir, "stderr"), c.proxy.stderr);
  writeFileSync(join(dir, "exit_code"), String(c.proxy.exit_code));
  if (c.proxy.hang) writeFileSync(join(dir, "hang"), "");
  return fake;
}

/** The case's pin: `installed` is the digest of the fake's bytes. */
function expectedSha256(c: Json): string | undefined {
  const pin = c.client?.expected_sha256;
  if (pin !== "installed") return pin;
  const bytes = readFileSync(join(FIXTURES, "fake-kei-proxy.sh"));
  return createHash("sha256").update(bytes).digest("hex");
}

/** A fresh temp dir with symlinks resolved (macOS /var is a symlink). */
function caseDir(id: string): string {
  const dir = realpathSync(mkdtempSync(join(tmpdir(), `kei-case-${id}-`)));
  dirs.push(dir);
  return dir;
}

function isAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

async function waitDead(pid: number, withinMs: number): Promise<boolean> {
  const deadline = Date.now() + withinMs;
  while (Date.now() < deadline) {
    if (!isAlive(pid)) return true;
    await new Promise((r) => setTimeout(r, 20));
  }
  return !isAlive(pid);
}

interface Run {
  decision: Decision;
  dir: string;
  logs: string[];
}

const dirs: string[] = [];
afterAll(() => {
  for (const d of dirs) rmSync(d, { recursive: true, force: true });
});

async function runCase(c: Json, extraEnv?: Record<string, string>): Promise<Run> {
  const dir = caseDir(c.id);
  const logs: string[] = [];
  const client = new KeiProxyAuthorizeClient({
    executable: installFake(c, dir),
    expectedSha256: expectedSha256(c),
    timeoutMs: TABLE.timeout_ms,
    env: buildEnv(c),
    extraEnv,
  });
  const evaluator = new KeiProxyEvaluator(client, {
    logger: (level, message) => logs.push(`${level} ${message}`),
  });
  const decision = await evaluator.evaluate(c.input.tool, c.input.args, buildCaller(c));
  return { decision, dir, logs };
}

function readChildEnv(dir: string): Record<string, string> {
  return Object.fromEntries(
    readFileSync(join(dir, "env"), "utf8")
      .split("\n")
      .filter((l) => l.includes("="))
      .map((l) => [l.slice(0, l.indexOf("=")), l.slice(l.indexOf("=") + 1)]),
  );
}

const posix = process.platform === "win32" ? describe.skip : describe;

posix("KeiProxyEvaluator authorize-cases.v1", () => {
  test.each(CASES.map((c) => [c.id, c] as const))("%s", async (_id, c) => {
    const expected = c.expected;
    const { decision, dir, logs } = await runCase(c);

    expect(decision.action).toBe(expected.action as Action);
    const klass = `kei-proxy ${expected.reason_class}`;
    expect(decision.reason === klass || decision.reason.startsWith(`${klass}:`)).toBe(true);
    expect(decision.rule).toBe(expected.rule);
    if (expected.enrollment === null) {
      expect(decision.enrollment).toBeUndefined();
    } else {
      expect(decision.enrollment).toEqual(expected.enrollment);
    }
    if (expected.reason_contains) expect(decision.reason).toContain(expected.reason_contains);

    const logText = logs.join("\n");
    for (const [name, canary] of Object.entries(CANARIES)) {
      expect({ name, leaked: decision.reason.includes(canary) }).toEqual({ name, leaked: false });
      expect({ name, leaked: logText.includes(canary) }).toEqual({ name, leaked: false });
    }

    const argvFile = join(dir, "argv");
    expect(existsSync(argvFile)).toBe(expected.invoked);
    if (expected.invoked) {
      const argv = readFileSync(argvFile, "utf8").split("\n").filter((l) => l !== "");
      expect(argv.some((a) => a.includes(CANARIES.runtime_token))).toBe(false);
      for (const flag of TABLE.argv_forbidden.flags as string[]) expect(argv).not.toContain(flag);
      if (expected.argv) expect(argv).toEqual(expected.argv);
    }
    if (expected.group_killed) {
      const pid = Number(readFileSync(join(dir, "hang_pid"), "utf8").trim());
      expect(await waitDead(pid, 2_000)).toBe(true);
    }
  });

  const allowCase = CASES.find((c) => c.id === "allow_member_read")!;

  test("child env carries the token and only allowlisted vars", async () => {
    const { dir } = await runCase(allowCase);
    const child = readChildEnv(dir);
    expect(child.KEI_RUNTIME_TOKEN).toBe(CANARIES.runtime_token);
    for (const name of TABLE.child_env.stripped as string[]) expect(child[name]).toBeUndefined();
    const shellAdded = new Set(["PWD", "SHLVL", "_", "OLDPWD"]);
    const allowed = new Set(TABLE.child_env.allowlist as string[]);
    for (const key of Object.keys(child)) {
      if (!shellAdded.has(key)) expect(allowed.has(key)).toBe(true);
    }
  });

  test("the child sees no PATH or HOME even when the parent has them", async () => {
    const { dir } = await runCase(allowCase);
    const child = readChildEnv(dir);
    expect(child.PATH).toBeUndefined();
    expect(child.HOME).toBeUndefined();
  });

  test("extra env is passed explicitly", async () => {
    const { dir } = await runCase(allowCase, { AWS_PROFILE: "fixture" });
    expect(readFileSync(join(dir, "env"), "utf8").split("\n")).toContain("AWS_PROFILE=fixture");
  });

  test("a bare executable name is resolved against the parent PATH", async () => {
    const dir = caseDir("bare-name");
    installFake(allowCase, dir);
    const client = new KeiProxyAuthorizeClient({
      executable: "kei-proxy",
      timeoutMs: TABLE.timeout_ms,
      env: { PATH: `/nonexistent:${dir}`, KEI_RUNTIME_TOKEN: CANARIES.runtime_token },
    });
    const decision = await new KeiProxyEvaluator(client).evaluate(
      allowCase.input.tool,
      allowCase.input.args,
      buildCaller(allowCase),
    );
    expect(decision.action).toBe(Action.ALLOW);
    expect(readChildEnv(dir).PATH).toBeUndefined();
  });

  test("authorize does not block the event loop", async () => {
    const hang = CASES.find((c) => c.id === "timeout")!;
    const dir = caseDir("event-loop");
    const client = new KeiProxyAuthorizeClient({
      executable: installFake(hang, dir),
      timeoutMs: 500,
      env: buildEnv(hang),
    });
    const events: string[] = [];
    const timer = new Promise<void>((r) =>
      setTimeout(() => {
        events.push("timer");
        r();
      }, 25),
    );
    const decision = new KeiProxyEvaluator(client, { logger: () => undefined })
      .evaluate(hang.input.tool, hang.input.args, buildCaller(hang))
      .then((d) => {
        events.push("decision");
        return d;
      });
    await Promise.all([timer, decision]);
    expect(events).toEqual(["timer", "decision"]);
    expect((await decision).reason).toMatch(/^kei-proxy proxy_timeout/);
  });

  test("a pin that stops matching after construction denies the next call", async () => {
    const pinned = CASES.find((c) => c.id === "pinned_executable_allow")!;
    const dir = caseDir("pin-swap");
    const fake = installFake(pinned, dir);
    const client = new KeiProxyAuthorizeClient({
      executable: fake,
      expectedSha256: expectedSha256(pinned),
      timeoutMs: TABLE.timeout_ms,
      env: buildEnv(pinned),
    });
    const evaluator = new KeiProxyEvaluator(client, { logger: () => undefined });
    const call = () => evaluator.evaluate(pinned.input.tool, pinned.input.args, buildCaller(pinned));
    expect((await call()).action).toBe(Action.ALLOW);
    writeFileSync(fake, readFileSync(fake, "utf8") + "\n# swapped\n");
    rmSync(join(dir, "argv"));
    const decision = await call();
    expect(decision.action).toBe(Action.DENY);
    expect(decision.reason).toMatch(/^kei-proxy pin_mismatch/);
    expect(existsSync(join(dir, "argv"))).toBe(false);
  });
});

describe("KeiProxyAuthorizeClient construction", () => {
  const pin = "a".repeat(64);

  test("a malformed pin is refused", () => {
    expect(
      () => new KeiProxyAuthorizeClient({ executable: "/usr/bin/kei-proxy", expectedSha256: "abc" }),
    ).toThrow(TypeError);
  });

  test("a pin requires an absolute executable path", () => {
    expect(
      () => new KeiProxyAuthorizeClient({ executable: "kei-proxy", expectedSha256: pin }),
    ).toThrow(TypeError);
  });
});

describe("KeiProxyEvaluator contract constants", () => {
  test("child env allowlist matches the fixture", () => {
    expect([...AUTHORIZE_CHILD_ENV_ALLOWLIST]).toEqual(TABLE.child_env.allowlist);
  });

  test("reason classes match the fixture", () => {
    expect([...KEI_PROXY_REASON_CLASSES]).toEqual(TABLE.reason_classes);
  });
});

describe("KeiProxyEvaluator injected client", () => {
  const caller: CallerContext = { user_id: "U123", invoking_subject: "U_HUMAN", trusted: false };

  test("a throwing client denies with proxy_error", async () => {
    const evaluator = new KeiProxyEvaluator(
      {
        authorize: () => {
          throw new Error("proxy unreachable");
        },
      },
      { logger: () => undefined },
    );
    const decision = await evaluator.evaluate("github.read", {}, caller);
    expect(decision.action).toBe(Action.DENY);
    expect(decision.reason).toBe("kei-proxy proxy_error: authorize failed: proxy unreachable");
  });

  test("a rejecting async client denies with proxy_error", async () => {
    const evaluator = new KeiProxyEvaluator(
      { authorize: () => Promise.reject(new Error("proxy unreachable")) },
      { logger: () => undefined },
    );
    const decision = await evaluator.evaluate("github.read", {}, caller);
    expect(decision.action).toBe(Action.DENY);
    expect(decision.reason).toBe("kei-proxy proxy_error: authorize failed: proxy unreachable");
  });

  test("a typed authorize error keeps its class", async () => {
    const evaluator = new KeiProxyEvaluator(
      {
        authorize: () => {
          throw new KeiProxyAuthorizeError("proxy_timeout", "no answer");
        },
      },
      { logger: () => undefined },
    );
    expect((await evaluator.evaluate("github.read", {}, caller)).reason).toBe(
      "kei-proxy proxy_timeout: no answer",
    );
  });

  test("the request carries the invoking subject and derived resources", async () => {
    const seen: KeiProxyAuthorizeRequest[] = [];
    const evaluator = new KeiProxyEvaluator({
      authorize: (req) => {
        seen.push(req);
        return { decision: "allow" };
      },
    });
    await evaluator.evaluate("github.read", { owner: "acme", repo: "pipe" }, caller);
    expect(seen[0].userId).toBe("U_HUMAN");
    expect(seen[0].resource).toBe("github:repo:acme/pipe");
    expect(seen[0].action).toBe("execute");
  });
});

describe("keiProxyResourcesTouched / keiProxyToolArgsDigest", () => {
  test("uses the type:kind:id triple", () => {
    expect(
      keiProxyResourcesTouched("github.comment", {
        owner: "acme",
        repo: "sales-pipeline",
        issue_number: 123,
      }),
    ).toEqual(["github:repo:acme/sales-pipeline", "github:issue:123"]);
  });

  test("underscore tool names yield the same resource type", () => {
    expect(keiProxyResourcesTouched("github_create_issue", { issue_number: 5 })).toEqual([
      "github:issue:5",
    ]);
  });

  test("digest matches Python's json.dumps(sort_keys, compact, ensure_ascii)", () => {
    // python: json.dumps({"e": "\U0001F600", "z": ["\u2028", 1, True, None]},
    //                    sort_keys=True, separators=(",", ":"))
    expect(keiProxyToolArgsDigest({ z: ["\u2028", 1, true, null], e: "\u{1F600}" })).toBe(
      "303e9fb3a4f9a08e4b959e3558a1705fe29840918a9605df263ef27eaabfa25f",
    );
  });
});

describe("KeiProxyEvaluator boundary", () => {
  test("the evaluator module never spawns the proxy", () => {
    const src = readFileSync(resolve(process.cwd(), "src", "kei", "evaluator.ts"), "utf8");
    expect(src).not.toContain("child_process");
    expect(src).not.toContain("from \"./authorizeClient");
  });

  test("the authorize client never uses a synchronous spawn", () => {
    const src = readFileSync(resolve(process.cwd(), "src", "kei", "authorizeClient.ts"), "utf8");
    expect(src).not.toMatch(/spawnSync|execSync|execFileSync/);
  });
});
