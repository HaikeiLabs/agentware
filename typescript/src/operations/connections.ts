import {
  newHTTP,
  type HTTP,
} from "@haikeilabs/connector-contracts/kei/connection/index";

/** Connection configuration is metadata; credentials are released per call. */
export type ConnectionKind =
  | "http_bearer"
  | "graphql_bearer"
  | "http_header"
  | "http_basic"
  | "sql_dsn"
  | "key_pair_jwt"
  | "token_exchange";

/** The requested connection kind is not supported by the TypeScript runner. */
export class UnsupportedConnection extends Error {
  constructor() {
    super("Unsupported connection kind in TypeScript OperationRunner");
    this.name = "UnsupportedConnection";
  }
}

/** Build an HTTP connection only after authorization and credential release. */
export function buildConnection(
  kind: ConnectionKind,
  baseURL: string,
  credential: string,
  headerName?: string,
): HTTP {
  switch (kind) {
    case "http_bearer":
    case "graphql_bearer":
      return newHTTP(baseURL, "bearer", credential);
    case "http_header":
      return newHTTP(baseURL, "header", credential, headerName);
    case "http_basic":
      return newHTTP(baseURL, "basic", credential);
    default:
      throw new UnsupportedConnection();
  }
}

/** SQL and the remaining connection kinds require a future runtime integration. */
export function assertSupportedConnection(kind: ConnectionKind): void {
  if (
    !["http_bearer", "graphql_bearer", "http_header", "http_basic"].includes(
      kind,
    )
  )
    throw new UnsupportedConnection();
}
