import { runHarness } from "./index.ts";

const fake = process.argv.includes("--fake");
let closeFake: (() => Promise<void>) | undefined;

if (fake) {
  const { startFakeProxy } = await import("./fake-proxy.ts");
  const proxy = await startFakeProxy();
  process.env.KEI_RUNTIME_SOCKET_PATH = proxy.socketPath;
  process.env.KEI_RUNTIME_TOKEN = "offline-example-token";
  closeFake = proxy.close;
}

try {
  await runHarness();
} finally {
  await closeFake?.();
}
