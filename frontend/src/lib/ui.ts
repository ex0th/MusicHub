// Kleine DOM-Helfer ohne Framework. Alle Texte laufen über textContent – kein innerHTML
// mit Nutzerdaten.

import { ApiError } from "./api";
import { coverage, SLOT_TEXT } from "./platforms";
import type { Track } from "./types";

type Child = Node | string | number | null | undefined | false;
type Attrs = Record<string, string | number | boolean | null | undefined | EventListener>;

export function h<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attrs: Attrs = {},
  ...children: Child[]
): HTMLElementTagNameMap[K] {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (typeof value === "function") {
      el.addEventListener(key.replace(/^on/, ""), value);
    } else if (value === true) {
      el.setAttribute(key, "");
    } else {
      el.setAttribute(key, String(value));
    }
  }
  append(el, ...children);
  return el;
}

export function append(parent: Element, ...children: Child[]): void {
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    parent.append(child instanceof Node ? child : String(child));
  }
}

export function $<T extends HTMLElement = HTMLElement>(selector: string, root: ParentNode = document): T {
  const el = root.querySelector<T>(selector);
  if (!el) throw new Error(`Element ${selector} fehlt`);
  return el;
}

/** Lesbare Fehlermeldung für die Oberfläche, inkl. Feldfehlern bei 422. */
export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    const fieldErrors = error.problem?.errors;
    if (fieldErrors?.length) {
      return fieldErrors
        .map((e) => {
          const field = e.loc.filter((part) => part !== "body").join(".");
          return field ? `${field}: ${e.msg}` : e.msg;
        })
        .join("; ");
    }
    if (error.status === 401) return "Nicht angemeldet. Bitte unter Einstellungen ein Token eingeben.";
    if (error.status === 403) return "Keine Schreibberechtigung.";
    return error.message;
  }
  if (error instanceof TypeError) return "Server nicht erreichbar.";
  return error instanceof Error ? error.message : "Unbekannter Fehler.";
}

// --- Rückmeldungen -------------------------------------------------------------

export function toast(message: string, kind: "ok" | "error" = "ok"): void {
  const region = document.getElementById("toasts");
  if (!region) return;
  const item = h("div", { class: `toast toast-${kind}`, role: kind === "error" ? "alert" : "status" }, message);
  region.append(item);
  window.setTimeout(() => item.remove(), kind === "error" ? 7000 : 3500);
}

/** Bestätigungsdialog mit dem nativen <dialog>. Löst true aus, wenn bestätigt. */
export function confirmDialog(options: {
  title: string;
  message: string;
  confirm: string;
  danger?: boolean;
}): Promise<boolean> {
  return new Promise((resolve) => {
    const dialog = h(
      "dialog",
      { class: "confirm" },
      h(
        "form",
        { method: "dialog" },
        h("h2", {}, options.title),
        h("p", {}, options.message),
        h(
          "div",
          { class: "dialog-actions" },
          h("button", { value: "cancel", class: "ghost" }, "Abbrechen"),
          h("button", { value: "ok", class: options.danger ? "danger" : "" }, options.confirm),
        ),
      ),
    );
    dialog.addEventListener("close", () => {
      resolve(dialog.returnValue === "ok");
      dialog.remove();
    });
    document.body.append(dialog);
    dialog.showModal();
  });
}

// --- Formatierung --------------------------------------------------------------

const dateFormat = new Intl.DateTimeFormat("de-DE", { dateStyle: "medium" });
const dateTimeFormat = new Intl.DateTimeFormat("de-DE", {
  dateStyle: "medium",
  timeStyle: "short",
});

export const formatDate = (iso: string | null): string =>
  iso ? dateFormat.format(new Date(iso)) : "–";
export const formatDateTime = (iso: string | null): string =>
  iso ? dateTimeFormat.format(new Date(iso)) : "–";

export function plural(n: number, one: string, many: string): string {
  return `${n.toLocaleString("de-DE")} ${n === 1 ? one : many}`;
}

// --- Abdeckungsstreifen --------------------------------------------------------

/**
 * Drei feste Felder (Spotify, SoundCloud, YouTube). Verfügbare Felder sind Links zur
 * Plattform; mit ``interactive: false`` nur Anzeige (z. B. in klickbaren Zeilen).
 */
export function coverageStrip(track: Pick<Track, "sources">, interactive = true): HTMLElement {
  const strip = h("span", { class: "coverage" });
  for (const slot of coverage(track)) {
    const label = `${slot.platform.label}: ${SLOT_TEXT[slot.state]}`;
    const url = slot.source?.url;
    const el =
      interactive && url && slot.state !== "off"
        ? h(
            "a",
            {
              class: "slot",
              "data-state": slot.state,
              href: url,
              target: "_blank",
              rel: "noopener noreferrer",
              title: `${label} – auf ${slot.platform.label} öffnen`,
              "aria-label": `${label}, auf ${slot.platform.label} öffnen`,
            },
            slot.platform.short,
          )
        : h(
            "span",
            { class: "slot", "data-state": slot.state, title: label, "aria-label": label, role: "img" },
            slot.platform.short,
          );
    strip.append(el);
  }
  return strip;
}

// --- URL-Parameter -------------------------------------------------------------

export function param(name: string): string | null {
  return new URLSearchParams(window.location.search).get(name);
}

// --- Downloads -------------------------------------------------------------------

/** Lädt eine geschützte Datei (Export, Bericht) und bietet sie zum Speichern an. */
export async function downloadFile(
  api: { download(path: string): Promise<{ blob: Blob; filename: string }> },
  path: string,
): Promise<void> {
  try {
    const { blob, filename } = await api.download(path);
    const url = URL.createObjectURL(blob);
    const link = h("a", { href: url, download: filename, hidden: true });
    document.body.append(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 30_000);
  } catch (e) {
    toast(errorMessage(e), "error");
  }
}

export function formatBytes(n: number): string {
  if (n >= 1024 * 1024) return `${(n / (1024 * 1024)).toLocaleString("de-DE", { maximumFractionDigits: 1 })} MB`;
  return `${Math.max(1, Math.round(n / 1024)).toLocaleString("de-DE")} KB`;
}
