/**
 * Snowflake key-pair JWT for the SQL API, minted on the harness side.
 *
 * The Kei runtime releases the Snowflake service user's private key (a PEM,
 * delivered as the released credential's access token) for one call. The
 * harness turns it into a short-lived JWT with {@link snowflakeJwt} and calls
 * Snowflake itself. This module makes no network calls, never logs, and never
 * puts key material or the token in an error; parser errors are not attached
 * as a cause.
 *
 * Token shape (Snowflake key-pair authentication):
 *
 *     header  {"alg":"RS256","typ":"JWT"}
 *     iss     <ACCOUNT>.<USER>.SHA256:<base64 SHA-256 of the public key DER>
 *     sub     <ACCOUNT>.<USER>
 *     iat     now, in Unix seconds
 *     exp     iat + ttl, capped at one hour
 *
 * Send it as `Authorization: Bearer <token>`; the optional
 * `X-Snowflake-Authorization-Token-Type: KEYPAIR_JWT` header names the type.
 */
import {
  createHash,
  createPrivateKey,
  createPublicKey,
  sign,
  type KeyObject,
} from "node:crypto";

/** The longest lifetime Snowflake honours for a key-pair JWT; longer is capped. */
export const SNOWFLAKE_JWT_MAX_TTL_MS = 60 * 60 * 1000;

/** The smallest RSA modulus accepted. */
export const SNOWFLAKE_JWT_MIN_RSA_BITS = 2048;

export type SnowflakeJwtErrorCode =
  | "invalid_account"
  | "invalid_user"
  | "invalid_ttl"
  | "invalid_now"
  | "invalid_key"
  | "encrypted_private_key";

const MESSAGES: Record<SnowflakeJwtErrorCode, string> = {
  invalid_account: "snowflake: invalid account identifier",
  invalid_user: "snowflake: invalid user",
  invalid_ttl: "snowflake: ttl must be a positive number of milliseconds",
  invalid_now: "snowflake: now must be a valid Date",
  invalid_key: "snowflake: invalid RSA key",
  encrypted_private_key:
    "snowflake: private key is encrypted; passphrase-protected keys are not supported",
};

/** Raised for every refusal. The message is fixed by `code` and never carries key material. */
export class SnowflakeJwtError extends Error {
  readonly code: SnowflakeJwtErrorCode;

  constructor(code: SnowflakeJwtErrorCode) {
    super(MESSAGES[code]);
    this.name = "SnowflakeJwtError";
    this.code = code;
  }
}

const HEADER = Buffer.from('{"alg":"RS256","typ":"JWT"}').toString("base64url");
const ACCOUNT_RE = /^[A-Z0-9_-]+$/;
// eslint-disable-next-line no-control-regex
const CONTROL_RE = /[\u0000-\u001f\u007f-\u009f]/;

/**
 * Return the account identifier in the form the JWT claims need.
 *
 * Trims and upper-cases the input. An account locator with region and cloud
 * suffixes (`xy12345.us-east-1.aws`) is cut at the first `.`, leaving the
 * locator. An identifier containing `.global` has every `.` replaced by `-`.
 * It is never cut at `-`, so the organization form is kept whole: pass it as
 * `ORG-ACCOUNT`, not `ORG.ACCOUNT` (the dotted form would be read as
 * locator.region). A host name (`...snowflakecomputing.com`) is refused.
 */
export function normalizeSnowflakeAccountIdentifier(id: string): string {
  let s = id.trim().toUpperCase();
  if (s.includes("SNOWFLAKECOMPUTING")) throw new SnowflakeJwtError("invalid_account");
  s = s.includes(".GLOBAL") ? s.replaceAll(".", "-") : s.split(".", 1)[0];
  if (!ACCOUNT_RE.test(s)) throw new SnowflakeJwtError("invalid_account");
  return s;
}

function normalizeUser(user: string): string {
  const s = user.trim().toUpperCase();
  if (s === "" || CONTROL_RE.test(s)) throw new SnowflakeJwtError("invalid_user");
  return s;
}

/**
 * Return `SHA256:` plus the base64 SHA-256 of the key's DER SubjectPublicKeyInfo.
 * Equals Snowflake's `RSA_PUBLIC_KEY_FP` and
 * `openssl rsa -pubout -outform DER | openssl dgst -sha256 -binary | base64`.
 *
 * `keyPem` is an RSA public key PEM (SubjectPublicKeyInfo or PKCS#1), or an
 * unencrypted private key PEM, whose public half is used.
 */
export function snowflakePublicKeyFingerprint(keyPem: string | Uint8Array): string {
  const text = pemText(keyPem);
  if (text.includes("-----BEGIN ENCRYPTED PRIVATE KEY-----") || text.includes("Proc-Type: 4,ENCRYPTED")) {
    throw new SnowflakeJwtError("encrypted_private_key");
  }
  let pub: KeyObject;
  try {
    pub = createPublicKey({ key: text, format: "pem" });
  } catch {
    throw new SnowflakeJwtError("invalid_key");
  }
  if (pub.asymmetricKeyType !== "rsa") throw new SnowflakeJwtError("invalid_key");
  return fingerprintOf(pub);
}

function fingerprintOf(pub: KeyObject): string {
  const der = pub.export({ type: "spki", format: "der" });
  return `SHA256:${createHash("sha256").update(der).digest("base64")}`;
}

function pemText(pem: string | Uint8Array): string {
  return typeof pem === "string" ? pem : new TextDecoder().decode(pem);
}

function loadPrivateKey(privateKeyPem: string | Uint8Array): KeyObject {
  const text = pemText(privateKeyPem);
  if (text.includes("-----BEGIN ENCRYPTED PRIVATE KEY-----") || text.includes("Proc-Type: 4,ENCRYPTED")) {
    throw new SnowflakeJwtError("encrypted_private_key");
  }
  if (!text.includes("-----BEGIN PRIVATE KEY-----") && !text.includes("-----BEGIN RSA PRIVATE KEY-----")) {
    throw new SnowflakeJwtError("invalid_key");
  }
  let key: KeyObject;
  try {
    key = createPrivateKey({ key: text, format: "pem" });
  } catch {
    throw new SnowflakeJwtError("invalid_key");
  }
  const bits = key.asymmetricKeyDetails?.modulusLength ?? 0;
  if (key.asymmetricKeyType !== "rsa" || bits < SNOWFLAKE_JWT_MIN_RSA_BITS) {
    throw new SnowflakeJwtError("invalid_key");
  }
  if (!rsaKeyIsConsistent(key)) throw new SnowflakeJwtError("invalid_key");
  return key;
}

const bigFromB64url = (s: unknown): bigint => {
  if (typeof s !== "string" || s === "") throw new Error("missing component");
  return BigInt(`0x${Buffer.from(s, "base64url").toString("hex")}`);
};

/**
 * OpenSSL loads an RSA private key without checking its components, so a
 * corrupted key could still load. Go (rsa.PrivateKey.Validate) and Python
 * (cryptography) reject such keys; this applies the same checks:
 * n = p*q, e*dp = 1 mod (p-1), e*dq = 1 mod (q-1), and qi*q = 1 mod p.
 */
function rsaKeyIsConsistent(key: KeyObject): boolean {
  try {
    const jwk = key.export({ format: "jwk" });
    const n = bigFromB64url(jwk.n);
    const e = bigFromB64url(jwk.e);
    const p = bigFromB64url(jwk.p);
    const q = bigFromB64url(jwk.q);
    const dp = bigFromB64url(jwk.dp);
    const dq = bigFromB64url(jwk.dq);
    const qi = bigFromB64url(jwk.qi);
    return (
      p > 1n &&
      q > 1n &&
      p * q === n &&
      (e * dp) % (p - 1n) === 1n &&
      (e * dq) % (q - 1n) === 1n &&
      (qi * q) % p === 1n
    );
  } catch {
    return false;
  }
}

/**
 * Mint a Snowflake key-pair JWT for `user` in `accountIdentifier`.
 *
 * `privateKeyPem` is an unencrypted PKCS#8 or PKCS#1 RSA key. `iat` is `now`
 * truncated to whole seconds and `exp` is `iat` plus `ttlMs` (whole seconds),
 * capped at {@link SNOWFLAKE_JWT_MAX_TTL_MS}. A ttl of zero or less is
 * refused. See {@link normalizeSnowflakeAccountIdentifier} for the account
 * rules; the user is trimmed and upper-cased.
 *
 * The returned token is a credential: never log it.
 */
export function snowflakeJwt(
  accountIdentifier: string,
  user: string,
  privateKeyPem: string | Uint8Array,
  now: Date,
  ttlMs: number,
): string {
  const account = normalizeSnowflakeAccountIdentifier(accountIdentifier);
  const subjectUser = normalizeUser(user);
  const nowMs = now.getTime();
  if (!Number.isFinite(nowMs)) throw new SnowflakeJwtError("invalid_now");
  if (!Number.isFinite(ttlMs) || ttlMs <= 0) throw new SnowflakeJwtError("invalid_ttl");
  const ttlSeconds = Math.floor(Math.min(ttlMs, SNOWFLAKE_JWT_MAX_TTL_MS) / 1000);
  const key = loadPrivateKey(privateKeyPem);

  const iat = Math.floor(nowMs / 1000);
  const subject = `${account}.${subjectUser}`;
  const claims = {
    iss: `${subject}.${fingerprintOf(createPublicKey(key))}`,
    sub: subject,
    iat,
    exp: iat + ttlSeconds,
  };
  const signingInput = `${HEADER}.${Buffer.from(JSON.stringify(claims)).toString("base64url")}`;
  const signature = sign("sha256", Buffer.from(signingInput), key);
  return `${signingInput}.${signature.toString("base64url")}`;
}
