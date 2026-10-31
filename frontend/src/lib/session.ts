import { ApiClient } from "./api";
import { DevTokenProvider, MemoryTokenProvider, type TokenProvider } from "./auth";

/** Gemeinsamer Einstiegspunkt für Seiten-Skripte: API-Client plus Token-Quelle. */
export function createSession(): { api: ApiClient; tokens: TokenProvider } {
  const tokens: TokenProvider =
    typeof sessionStorage === "undefined"
      ? new MemoryTokenProvider()
      : new DevTokenProvider(sessionStorage);
  return { api: new ApiClient({ tokens }), tokens };
}
