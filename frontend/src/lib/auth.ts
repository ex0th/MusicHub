// Authentifizierung im Browser.
//
// Die API erwartet ein Bearer-Token. Woher es kommt, kapselt ein TokenProvider:
//  * DevTokenProvider – Token wird einmal eingegeben und nur in sessionStorage gehalten
//    (verschwindet mit dem Tab). Für MUSICHUB_AUTH_MODE=dev.
//  * OIDC (Authentik) – folgt laut Roadmap als OidcTokenProvider mit Authorization Code
//    Flow + PKCE; der Rest der App bleibt unverändert, weil er nur getToken() kennt.

export interface TokenProvider {
  getToken(): string | null;
  setToken(token: string | null): void;
}

const STORAGE_KEY = "musichub.devToken";

export class DevTokenProvider implements TokenProvider {
  constructor(private readonly storage: Pick<Storage, "getItem" | "setItem" | "removeItem">) {}

  getToken(): string | null {
    try {
      return this.storage.getItem(STORAGE_KEY);
    } catch {
      return null; // z. B. blockierter Storage
    }
  }

  setToken(token: string | null): void {
    try {
      if (token) this.storage.setItem(STORAGE_KEY, token);
      else this.storage.removeItem(STORAGE_KEY);
    } catch {
      /* Storage nicht verfügbar – Token gilt dann nur bis zum Neuladen nicht */
    }
  }
}

/** Fallback ohne Browser-Storage (Tests, SSR). */
export class MemoryTokenProvider implements TokenProvider {
  private token: string | null = null;
  getToken(): string | null {
    return this.token;
  }
  setToken(token: string | null): void {
    this.token = token;
  }
}
