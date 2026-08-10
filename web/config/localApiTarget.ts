const LOCAL_API_HOSTS = new Set(["127.0.0.1", "localhost", "::1"]);

export type LocalApiTarget = {
  host: string;
  port: number;
  origin: string;
};

export function resolveLocalApiTarget(
  loadedEnvironment: Readonly<Record<string, string | undefined>>,
  processEnvironment: Readonly<Record<string, string | undefined>>,
): LocalApiTarget {
  const host = processEnvironment.RSFMRI_HOST ?? loadedEnvironment.RSFMRI_HOST ?? "127.0.0.1";
  if (!LOCAL_API_HOSTS.has(host)) {
    throw new Error(
      "RSFMRI_HOST must be one of 127.0.0.1, localhost, or ::1 for the local-only development proxy",
    );
  }

  const rawPort = processEnvironment.RSFMRI_PORT ?? loadedEnvironment.RSFMRI_PORT ?? "8000";
  if (!/^[1-9]\d*$/.test(rawPort)) {
    throw new Error("RSFMRI_PORT must be an integer between 1 and 65535");
  }
  const port = Number(rawPort);
  if (!Number.isSafeInteger(port) || port > 65_535) {
    throw new Error("RSFMRI_PORT must be an integer between 1 and 65535");
  }

  const uriHost = host === "::1" ? `[${host}]` : host;
  return { host, port, origin: `http://${uriHost}:${port}` };
}
