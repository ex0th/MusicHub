import { ApiClient, ApiError } from "./api";
import { BrowserAuth, MemoryTokenProvider, loginUrl, type TokenProvider } from "./auth";
import { h } from "./ui";

/** Gemeinsamer Einstiegspunkt für Seiten-Skripte: API-Client plus Anmeldedaten. */
export function createSession(): { api: ApiClient; tokens: TokenProvider } {
  const tokens: TokenProvider =
    typeof sessionStorage === "undefined" || typeof document === "undefined"
      ? new MemoryTokenProvider()
      : new BrowserAuth(sessionStorage, () => document.cookie);
  const api = new ApiClient({ tokens, onUnauthorized: (error) => sessionLost(tokens, error) });
  return { api, tokens };
}

export function hasCredentials(tokens: TokenProvider): boolean {
  return tokens.hasCredentials ? tokens.hasCredentials() : tokens.getToken() !== null;
}

function loginLink(label: string): HTMLElement {
  return h("a", { class: "button", href: loginUrl(location) }, label);
}

/**
 * Ohne Anmeldung zeigt die Seite statt ihres Inhalts einen Hinweis mit Anmelde-Knopf.
 * Gibt true zurück, wenn Anmeldedaten vorhanden sind. Ob die Sitzung noch gilt, prüft
 * der erste API-Aufruf (401 → Hinweis „Sitzung abgelaufen“).
 */
export function requireLogin(tokens: TokenProvider, content: HTMLElement): boolean {
  if (hasCredentials(tokens)) return true;
  content.replaceChildren(
    h(
      "div",
      { class: "notice" },
      h("p", {}, "Melde dich an, um deine Bibliothek zu sehen."),
      loginLink("Anmelden"),
    ),
  );
  return false;
}

let lostShown = false;

/** Sitzung abgelaufen oder Token ungültig: einmalig ein Hinweis mit erneuter Anmeldung. */
function sessionLost(tokens: TokenProvider, error: ApiError): void {
  if (lostShown || typeof document === "undefined") return;
  lostShown = true;
  const expired = error.problem?.code === "session_expired";
  if (tokens.getToken()) tokens.setToken(null); // ungültiges dev-Token verwerfen
  const banner = h(
    "div",
    { class: "session-lost", role: "alert" },
    h(
      "p",
      {},
      expired
        ? "Deine Sitzung ist abgelaufen. Nicht gespeicherte Eingaben bleiben auf der Seite, bis du sie verlässt."
        : "Du bist nicht (mehr) angemeldet.",
    ),
    loginLink("Erneut anmelden"),
  );
  document.body.append(banner);
}

/** Abmelden: Sitzung beenden (oidc) bzw. Token verwerfen (dev) und weiterleiten. */
export async function logout(api: ApiClient, tokens: TokenProvider): Promise<void> {
  if (tokens.getToken()) {
    tokens.setToken(null);
    location.href = "/settings/";
    return;
  }
  try {
    const { redirect } = await api.logout();
    location.href = redirect;
  } catch (err) {
    // Sitzung war schon weg: trotzdem zur Login-Seite
    if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
      location.href = "/login/?logged_out=1";
      return;
    }
    throw err;
  }
}
