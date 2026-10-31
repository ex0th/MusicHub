import { ApiClient } from "./api";
import { DevTokenProvider, MemoryTokenProvider, type TokenProvider } from "./auth";
import { h } from "./ui";

/** Gemeinsamer Einstiegspunkt für Seiten-Skripte: API-Client plus Token-Quelle. */
export function createSession(): { api: ApiClient; tokens: TokenProvider } {
  const tokens: TokenProvider =
    typeof sessionStorage === "undefined"
      ? new MemoryTokenProvider()
      : new DevTokenProvider(sessionStorage);
  return { api: new ApiClient({ tokens }), tokens };
}

/**
 * Ohne Token zeigt die Seite statt ihres Inhalts einen Hinweis auf die Anmeldung.
 * Gibt true zurück, wenn ein Token vorhanden ist.
 */
export function requireLogin(tokens: TokenProvider, content: HTMLElement): boolean {
  if (tokens.getToken()) return true;
  content.replaceChildren(
    h(
      "div",
      { class: "notice" },
      h("p", {}, "Melde dich an, um deine Bibliothek zu sehen."),
      h("a", { class: "button", href: "/settings/" }, "Zur Anmeldung"),
    ),
  );
  return false;
}
