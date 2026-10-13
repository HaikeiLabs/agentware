import { createServer, type Server } from "node:http";
import { connect, type AddressInfo, type Socket } from "node:net";
import { inspect } from "node:util";
import {
  KeiProxyAuthorizeClient,
  KeiProxySocketAuthorizationClient,
  KeiProxySocketAuthorizeClient,
  ToolManifestSyncConfig,
  bypassesProxy,
  manifestToolCount,
  selectAuthorizationClient,
  syncToolManifest,
} from "../src/index.js";

const TOKEN = "kei_rt_unit-canary-token-0123456789";
const MANIFEST = { schema: "kei.tool-manifest/v2", tools: [{ name: "github.get_issue" }] };

function listen(server: Server): Promise<string> {
  return new Promise((resolve) =>
    server.listen(0, "127.0.0.1", () => resolve(`127.0.0.1:${(server.address() as AddressInfo).port}`)),
  );
}

function close(server: Server): Promise<void> {
  server.closeAllConnections();
  return new Promise((r) => server.close(() => r()));
}

describe("tool sync through an egress proxy", () => {
  let controlPlane: Server;
  let proxy: Server;
  let tunnels: string[];
  let puts: string[];

  beforeEach(() => {
    tunnels = [];
    puts = [];
    controlPlane = createServer((req, res) => {
      puts.push(`${req.method} ${req.url} ${req.headers.authorization}`);
      req.resume();
      req.on("end", () => res.writeHead(200, { "Content-Type": "application/json" }).end('{"updated":1}'));
    });
    // A minimal CONNECT proxy: records the authority and pipes bytes through.
    proxy = createServer((_req, res) => res.writeHead(405).end());
    proxy.on("connect", (req, client: Socket, head: Buffer) => {
      tunnels.push(`${req.url} ${req.headers["proxy-authorization"] ?? ""}`);
      const [host, port] = String(req.url).split(":");
      const upstream = connect(Number(port), host, () => {
        client.write("HTTP/1.1 200 Connection Established\r\n\r\n");
        upstream.write(head);
        upstream.pipe(client);
        client.pipe(upstream);
      });
      upstream.on("error", () => client.destroy());
      client.on("error", () => upstream.destroy());
    });
  });
  afterEach(async () => {
    await close(controlPlane);
    await close(proxy);
  });

  it("tunnels the PUT through KEI_PROXY_EGRESS_PROXY with its credentials", async () => {
    const target = await listen(controlPlane);
    const via = await listen(proxy);
    const config = ToolManifestSyncConfig.fromEnv({
      KEI_RUNTIME_CONTROL_PLANE_URL: `http://${target}`,
      KEI_RUNTIME_TOKEN: TOKEN,
      KEI_PROXY_EGRESS_PROXY: `http://user:p%40ss@${via}`,
    });
    const result = await syncToolManifest(MANIFEST, config);
    expect(result).toEqual({ sent: true, toolCount: 1, counts: { updated: 1 } });
    expect(tunnels).toEqual([`${target} Basic ${Buffer.from("user:p@ss").toString("base64")}`]);
    expect(puts).toEqual([`PUT /api/v1/runtime/tools Bearer ${TOKEN}`]);
  });

  it("goes direct when KEI_PROXY_NO_PROXY bypasses the control plane", async () => {
    const target = await listen(controlPlane);
    const via = await listen(proxy);
    const config = ToolManifestSyncConfig.fromEnv({
      KEI_RUNTIME_CONTROL_PLANE_URL: `http://${target}`,
      KEI_RUNTIME_TOKEN: TOKEN,
      KEI_PROXY_EGRESS_PROXY: `http://${via}`,
      KEI_PROXY_NO_PROXY: "127.0.0.1",
    });
    await syncToolManifest(MANIFEST, config);
    expect(tunnels).toEqual([]);
    expect(puts).toHaveLength(1);
  });

  it("uses the ambient HTTP_PROXY when no egress proxy is set", async () => {
    const target = await listen(controlPlane);
    const via = await listen(proxy);
    const config = new ToolManifestSyncConfig({
      controlPlaneUrl: `http://${target}`,
      token: TOKEN,
      env: { HTTP_PROXY: `http://${via}` },
    });
    await syncToolManifest(MANIFEST, config);
    expect(tunnels).toEqual([`${target} `]);
  });

  it("retries when the proxy refuses the tunnel, and never logs the token", async () => {
    const target = await listen(controlPlane);
    proxy.removeAllListeners("connect");
    proxy.on("connect", (_req, client: Socket) => client.end("HTTP/1.1 403 Forbidden\r\n\r\n"));
    const via = await listen(proxy);
    const logs: string[] = [];
    const config = new ToolManifestSyncConfig({
      controlPlaneUrl: `http://${target}`,
      token: TOKEN,
      egressProxy: `http://${via}`,
      attempts: 2,
    });
    await expect(
      syncToolManifest(MANIFEST, config, { sleep: async () => undefined, logger: (l, m) => logs.push(`${l} ${m}`) }),
    ).rejects.toThrow("network error: PROXY_403");
    expect(logs).toHaveLength(1);
    expect(logs.join("\n")).not.toContain(TOKEN);
    expect(puts).toEqual([]);
  });
});

describe("ToolManifestSyncConfig", () => {
  it("keeps the token out of JSON and inspect", () => {
    const config = ToolManifestSyncConfig.fromEnv({
      KEI_RUNTIME_CONTROL_PLANE_URL: "https://kei.example.test",
      KEI_RUNTIME_TOKEN: TOKEN,
    });
    expect(JSON.stringify(config)).not.toContain(TOKEN);
    expect(inspect(config, { showHidden: true, depth: 5 })).not.toContain(TOKEN);
    expect(String(Object.values(config))).not.toContain(TOKEN);
  });

  it("matches NO_PROXY entries by host or domain suffix only", () => {
    expect(bypassesProxy("kei.example.test", "example.test")).toBe(true);
    expect(bypassesProxy("kei.example.test", ".example.test:443")).toBe(true);
    expect(bypassesProxy("kei.example.test", "ample.test")).toBe(false);
    expect(bypassesProxy("kei.example.test", "")).toBe(false);
  });

  it("counts manifest tools", () => {
    expect(manifestToolCount(MANIFEST)).toBe(1);
    expect(manifestToolCount('{"tools": []}')).toBe(0);
    expect(manifestToolCount({})).toBe(0);
  });
});

describe("selectAuthorizationClient", () => {
  it("falls back to the kei-proxy subprocess client when no socket is configured", () => {
    expect(selectAuthorizationClient("teams", { env: {} })).toBeInstanceOf(KeiProxyAuthorizeClient);
  });

  it("uses the socket for an explicit path or KEI_RUNTIME_SOCKET_PATH", () => {
    const explicit = selectAuthorizationClient("teams", { socketPath: "/tmp/a.sock", env: {} });
    expect(explicit).toBeInstanceOf(KeiProxySocketAuthorizationClient);
    expect((explicit as KeiProxySocketAuthorizationClient).client.socketPath).toBe("/tmp/a.sock");
    const fromEnv = selectAuthorizationClient("teams", { env: { KEI_RUNTIME_SOCKET_PATH: "/tmp/b.sock" } });
    expect((fromEnv as KeiProxySocketAuthorizationClient).client.socketPath).toBe("/tmp/b.sock");
  });

  it("refuses an empty source and a call without a subject", async () => {
    expect(() => new KeiProxySocketAuthorizationClient(" ")).toThrow(TypeError);
    const adapter = new KeiProxySocketAuthorizationClient(
      "teams",
      new KeiProxySocketAuthorizeClient({ socketPath: "/tmp/none.sock", env: { KEI_RUNTIME_TOKEN: TOKEN } }),
    );
    await expect(adapter.authorize({ userId: "", tool: "t" } as never)).rejects.toMatchObject({
      reasonClass: "proxy_error",
    });
  });

  it("defaults the client socket path to /run/kei-proxy/runtime.sock", () => {
    expect(new KeiProxySocketAuthorizeClient({ env: {} }).socketPath).toBe("/run/kei-proxy/runtime.sock");
  });
});
