/**
 * Snowflake key-pair JWT helper (fixtures/snowflake/cases.v1.json).
 *
 * The same cases run in the Go and Python ports.
 */
import {
  createPublicKey,
  generateKeyPairSync,
  verify,
  type KeyObject,
} from "node:crypto";
import { readFileSync } from "node:fs";
import { join, resolve } from "node:path";
import {
  normalizeSnowflakeAccountIdentifier,
  SNOWFLAKE_JWT_MAX_TTL_MS,
  SnowflakeJwtError,
  snowflakeJwt,
  snowflakePublicKeyFingerprint,
} from "../src/index.js";

type Json = Record<string, any>;

const FIXTURES = resolve(process.cwd(), "..", "fixtures", "snowflake");
const fixture = (name: string): string => readFileSync(join(FIXTURES, name), "utf8");
const CASES: Json = JSON.parse(fixture("cases.v1.json"));
const KEYS: Record<string, string> = CASES.keys;
const NOW = new Date(1700000000 * 1000);
const MINUTE = 60_000;

const publicKey = (): KeyObject => createPublicKey(fixture(KEYS.public));

function split(token: string): { header: Json; claims: Json; signed: string; sig: Buffer } {
  const parts = token.split(".");
  expect(parts).toHaveLength(3);
  for (const p of parts) expect(p).toMatch(/^[A-Za-z0-9_-]+$/);
  return {
    header: JSON.parse(Buffer.from(parts[0], "base64url").toString("utf8")),
    claims: JSON.parse(Buffer.from(parts[1], "base64url").toString("utf8")),
    signed: `${parts[0]}.${parts[1]}`,
    sig: Buffer.from(parts[2], "base64url"),
  };
}

const verifies = (key: KeyObject, signed: string, sig: Buffer): boolean =>
  verify("sha256", Buffer.from(signed), key, sig);

const pkcs8 = (key: KeyObject): string => key.export({ type: "pkcs8", format: "pem" }).toString();

function codeOf(fn: () => unknown): string {
  try {
    fn();
  } catch (err) {
    expect(err).toBeInstanceOf(SnowflakeJwtError);
    return (err as SnowflakeJwtError).code;
  }
  throw new Error("expected a SnowflakeJwtError");
}

describe("snowflake key-pair JWT", () => {
  it("fingerprint matches openssl for the public and private PEM", () => {
    for (const keyFile of [KEYS.public, KEYS.pkcs8, KEYS.pkcs1]) {
      expect(snowflakePublicKeyFingerprint(fixture(keyFile))).toBe(CASES.expected_fingerprint);
    }
    expect(snowflakePublicKeyFingerprint(new TextEncoder().encode(fixture(KEYS.public)))).toBe(
      CASES.expected_fingerprint,
    );
  });

  it("fingerprint refuses non-RSA, garbage and encrypted input", () => {
    const ec = generateKeyPairSync("ec", { namedCurve: "P-256" }).publicKey;
    const ecPem = ec.export({ type: "spki", format: "pem" }).toString();
    expect(codeOf(() => snowflakePublicKeyFingerprint(ecPem))).toBe("invalid_key");
    expect(codeOf(() => snowflakePublicKeyFingerprint("not a key"))).toBe("invalid_key");
    expect(codeOf(() => snowflakePublicKeyFingerprint(fixture(KEYS.encrypted_pkcs8)))).toBe(
      "encrypted_private_key",
    );
  });

  for (const keyFile of [KEYS.pkcs8, KEYS.pkcs1]) {
    for (const tc of CASES.claims as Json[]) {
      it(`claims exact: ${keyFile} / ${tc.name}`, () => {
        const token = snowflakeJwt(
          tc.account,
          tc.user,
          fixture(keyFile),
          new Date(tc.now_unix * 1000),
          tc.ttl_seconds * 1000,
        );
        const { header, claims, signed, sig } = split(token);
        expect(header).toEqual({ alg: "RS256", typ: "JWT" });
        expect(claims).toEqual({ iss: tc.iss, sub: tc.sub, iat: tc.iat, exp: tc.exp });
        expect(verifies(publicKey(), signed, sig)).toBe(true);
      });
    }
  }

  it("max ttl is one hour", () => {
    expect(SNOWFLAKE_JWT_MAX_TTL_MS).toBe(3_600_000);
  });

  it("truncates sub-second now", () => {
    const token = snowflakeJwt("xy12345", "u", fixture(KEYS.pkcs8), new Date(1700000000999), MINUTE);
    const { claims } = split(token);
    expect([claims.iat, claims.exp]).toEqual([1700000000, 1700000060]);
  });

  it("refuses an invalid date", () => {
    expect(codeOf(() => snowflakeJwt("xy12345", "u", fixture(KEYS.pkcs8), new Date(NaN), MINUTE))).toBe(
      "invalid_now",
    );
  });

  it("signature verifies with a generated key and not with another", () => {
    const { privateKey, publicKey: pub } = generateKeyPairSync("rsa", { modulusLength: 2048 });
    const token = snowflakeJwt("myorg-myaccount", "svc", pkcs8(privateKey), new Date(), 10 * MINUTE);
    const { claims, signed, sig } = split(token);
    expect(verifies(pub, signed, sig)).toBe(true);
    const pubPem = pub.export({ type: "spki", format: "pem" }).toString();
    expect(claims.iss).toBe(`MYORG-MYACCOUNT.SVC.${snowflakePublicKeyFingerprint(pubPem)}`);
    const other = generateKeyPairSync("rsa", { modulusLength: 2048 });
    expect(verifies(other.publicKey, signed, sig)).toBe(false);
  });

  it("accepts a byte PEM", () => {
    const token = snowflakeJwt("xy12345", "u", new TextEncoder().encode(fixture(KEYS.pkcs8)), NOW, MINUTE);
    expect(split(token).claims.sub).toBe("XY12345.U");
  });

  for (const seconds of CASES.invalid_ttl_seconds as number[]) {
    it(`rejects ttl ${seconds}s`, () => {
      expect(codeOf(() => snowflakeJwt("xy12345", "u", fixture(KEYS.pkcs8), NOW, seconds * 1000))).toBe(
        "invalid_ttl",
      );
    });
  }

  it("rejects a non-finite ttl", () => {
    expect(codeOf(() => snowflakeJwt("xy12345", "u", fixture(KEYS.pkcs8), NOW, Number.NaN))).toBe(
      "invalid_ttl",
    );
  });

  for (const keyFile of [KEYS.encrypted_pkcs8, KEYS.encrypted_pkcs1]) {
    it(`rejects encrypted PEM ${keyFile}`, () => {
      let message = "";
      const code = codeOf(() => {
        try {
          snowflakeJwt("xy12345", "u", fixture(keyFile), NOW, MINUTE);
        } catch (err) {
          message = (err as Error).message;
          throw err;
        }
      });
      expect(code).toBe("encrypted_private_key");
      expect(message).toContain("encrypted");
      expect(message).toContain("passphrase");
    });
  }

  it("rejects invalid keys", () => {
    const small = generateKeyPairSync("rsa", { modulusLength: 1024 }).privateKey;
    const ec = generateKeyPairSync("ec", { namedCurve: "P-256" }).privateKey;
    const inputs: Record<string, string> = {
      empty: "",
      "not pem": "not a key",
      "public key": fixture(KEYS.public),
      "rsa 1024": pkcs8(small),
      "ec p256": pkcs8(ec),
      "garbage der": "-----BEGIN PRIVATE KEY-----\nanVuaw==\n-----END PRIVATE KEY-----\n",
    };
    for (const pem of Object.values(inputs)) {
      expect(codeOf(() => snowflakeJwt("xy12345", "u", pem, NOW, MINUTE))).toBe("invalid_key");
    }
  });

  it("errors never carry key material", () => {
    const key = fixture(KEYS.pkcs8);
    const body = key
      .split("\n")
      .filter((l) => l && !l.startsWith("-----"))
      .join("");
    const der = Buffer.from(body, "base64");
    der[Math.floor(der.length / 2)] ^= 0xff;
    const bad = `-----BEGIN PRIVATE KEY-----\n${der.toString("base64")}\n-----END PRIVATE KEY-----\n`;
    const attempts: Array<() => unknown> = [
      () => snowflakeJwt("xy12345", "u", bad, NOW, MINUTE),
      () => snowflakeJwt("xy12345", "u", key.slice(0, key.length / 2), NOW, MINUTE),
      () => snowflakeJwt("xy12345", "u", fixture(KEYS.encrypted_pkcs8), NOW, MINUTE),
      () => snowflakeJwt("", "u", key, NOW, MINUTE),
      () => snowflakeJwt("xy12345", "u", key, NOW, 0),
    ];
    const mid = Math.floor(body.length / 2);
    for (const attempt of attempts) {
      let caught: unknown;
      try {
        attempt();
      } catch (err) {
        caught = err;
      }
      expect(caught).toBeInstanceOf(SnowflakeJwtError);
      const err = caught as SnowflakeJwtError & { cause?: unknown };
      const rendered = `${err.message} ${String(err)} ${JSON.stringify(err)} ${err.stack ?? ""}`;
      expect(rendered).not.toContain("BEGIN");
      expect(rendered).not.toContain("PRIVATE");
      expect(rendered).not.toContain(body.slice(0, 32));
      expect(rendered).not.toContain(body.slice(mid, mid + 32));
      expect(err.cause).toBeUndefined();
    }
  });

  for (const tc of CASES.accounts as Json[]) {
    it(`normalizes account ${JSON.stringify(tc.in)}`, () => {
      expect(normalizeSnowflakeAccountIdentifier(tc.in)).toBe(tc.out);
    });
  }

  for (const value of CASES.invalid_accounts as string[]) {
    it(`rejects account ${JSON.stringify(value)}`, () => {
      expect(codeOf(() => normalizeSnowflakeAccountIdentifier(value))).toBe("invalid_account");
    });
  }

  for (const tc of CASES.users as Json[]) {
    it(`normalizes user ${JSON.stringify(tc.in)}`, () => {
      const token = snowflakeJwt("xy12345", tc.in, fixture(KEYS.pkcs8), NOW, MINUTE);
      expect(split(token).claims.sub).toBe(`XY12345.${tc.out}`);
    });
  }

  for (const value of CASES.invalid_users as string[]) {
    it(`rejects user ${JSON.stringify(value)}`, () => {
      expect(codeOf(() => snowflakeJwt("xy12345", value, fixture(KEYS.pkcs8), NOW, MINUTE))).toBe(
        "invalid_user",
      );
    });
  }
});
