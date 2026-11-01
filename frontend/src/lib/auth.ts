// Authentifizierung im Browser.
//
// Zwei Wege, je nach MUSICHUB_AUTH_MODE des Backends:
//  * oidc – Anmeldung über Authentik (/api/auth/login). Das Backend hält die Sitzung
//    serverseitig; der Browser hat nur ein HttpOnly-Cookie. JavaScript sieht davon nur den
//    CSRF-Token (eigenes Cookie), der bei verändernden Anfragen als Header mitgeht.
//  * dev  – statisches Entwicklungs-Token, nur in sessionStorage (verschwindet mit dem Tab).
//
// Der Rest der App kennt nur diese Schnittstelle.

export interface TokenProvider {
  /** Bearer-Token (nur dev-Modus). */
  getToken(): string | null;
  setToken(token: string | null): void;
  /** CSRF-Token der Cookie-Sitzung (oidc). */
  csrfToken?(): string | null;
  /** Gibt es überhaupt Anmeldedaten (Token oder Sitzung)? */
  hasCredentials?(): boolean;
}

const STORAGE_KEY = "musichub.devToken";
/** Mit HTTPS heißt das Cookie __Host-musichub_csrf, lokal ohne HTTPS musichub_csrf. */
export const CSRF_COOKIES = ["__Host-musichub_csrf", "musichub_csrf"] as const;

type StorageLike = Pick<Storage, "getItem" | "setItem" | "removeItem">;

export function readCookie(cookieString: string, name: string): string | null {
  for (const part of cookieString.split(";")) {
    const index = part.indexOf("=");
    if (index < 0) continue;
    if (part.slice(0, index).trim() === name) {
      const value = part.slice(index + 1).trim();
      return value ? decodeURIComponent(value) : null;
    }
  }
  return null;
}

export class DevTokenProvider implements TokenProvider {
  constructor(private readonly storage: StorageLike) {}

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

  hasCredentials(): boolean {
    return this.getToken() !== null;
  }
}

/** Browser: dev-Token (falls gesetzt) oder Cookie-Sitzung. */
export class BrowserAuth extends DevTokenProvider {
  constructor(
    storage: StorageLike,
    private readonly cookies: () => string,
  ) {
    super(storage);
  }

  csrfToken(): string | null {
    const all = this.cookies();
    for (const name of CSRF_COOKIES) {
      const value = readCookie(all, name);
      if (value) return value;
    }
    return null;
  }

  override hasCredentials(): boolean {
    return this.getToken() !== null || this.csrfToken() !== null;
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
  hasCredentials(): boolean {
    return this.token !== null;
  }
}

/** Pfad für die Anmeldung mit Rückkehr zur aktuellen Seite. */
export function loginUrl(current: { pathname: string; search: string }): string {
  const next = `${current.pathname}${current.search}`;
  return `/api/auth/login?next=${encodeURIComponent(next)}`;
}

/** Fehlerkennungen aus /api/auth/callback bzw. /api/auth/login (?error=…). */
export const LOGIN_ERRORS: Record<string, string> = {
  not_configured:
    "Die Anmeldung über Authentik ist nicht eingerichtet (MUSICHUB_OIDC_CLIENT_ID, MUSICHUB_OIDC_CLIENT_SECRET, MUSICHUB_PUBLIC_URL).",
  provider_unavailable: "Authentik ist gerade nicht erreichbar. Bitte später erneut versuchen.",
  invalid_state:
    "Die Anmeldung ist abgelaufen oder wurde in einem anderen Browser gestartet. Bitte erneut anmelden.",
  access_denied: "Die Anmeldung wurde abgebrochen.",
  provider_error: "Authentik hat die Anmeldung abgelehnt.",
  invalid_request: "Die Antwort von Authentik war unvollständig.",
  token_exchange_failed:
    "Authentik hat den Anmeldecode nicht bestätigt. Prüfe Client-ID, Client-Secret und Redirect-URI.",
  invalid_id_token:
    "Das Anmelde-Token von Authentik war ungültig (Signaturschlüssel, Issuer oder Uhrzeit prüfen).",
  forbidden: "Dein Konto hat keinen Zugriff auf MusicHub.",
};

export function loginErrorText(code: string | null): string | null {
  if (!code) return null;
  return LOGIN_ERRORS[code] ?? "Die Anmeldung ist fehlgeschlagen.";
}
