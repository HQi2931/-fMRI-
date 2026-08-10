import { useEffect, useState } from "react";

import { api } from "../api/client";

export type ConnectionState = "checking" | "online" | "offline";

export function useApiHealth(): ConnectionState {
  const [state, setState] = useState<ConnectionState>("checking");

  useEffect(() => {
    const controller = new AbortController();
    api
      .health(controller.signal)
      .then(() => setState("online"))
      .catch((error: unknown) => {
        if (!(error instanceof DOMException && error.name === "AbortError")) setState("offline");
      });
    return () => controller.abort();
  }, []);

  return state;
}
