import { describe, expect, it, vi } from "vitest";

import { ApiClient, ApiError } from "./api";
import { BrowserAuth, LOGIN_ERRORS, loginErrorText, loginUrl, readCookie } from "./auth";

function memoryStorage(): Pick<Storage, "getItem" | "setItem" | "removeItem"> {
  const data = new Map<string, string>();
  return {
    getItem: (k) => data.get(k) ?? null,
    setItem: (k, v) => void data.set(k, v),
    removeItem: (k) => void data.delete(k),
  };
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/problem+json" },
  });
}

describe("Cookies", () => {
  it("liest einzelne Cookies", () => {
    const all = "a=1; __Host-musichub_csrf=abc%3D; b=";
    expect(readCookie(all, "__Host-musichub_csrf")).toBe("abc=");
    expect(readCookie(all, "b")).toBeNull();
    expect(readCookie(all, "fehlt")).toBeNull();
  });
});

describe("BrowserAuth", () => {
  it("erkennt die Cookie-Sitzung am CSRF-Cookie (mit und ohne __Host-)", () => {
    let cookies = "";
    const auth = new BrowserAuth(memoryStorage(), () => cookies);
    expect(auth.hasCredentials()).toBe(false);
    cookies = "musichub_csrf=t1";
    expect(auth.csrfToken()).toBe("t1");
    cookies = "__Host-musichub_csrf=t2";
    expect(auth.csrfToken()).toBe("t2");
    expect(auth.hasCredentials()).toBe(true);
  });

  it("sendet den CSRF-Token nur bei verändernden Anfragen und kein Bearer-Token", async () => {
    const auth = new BrowserAuth(memoryStorage(), () => "__Host-musichub_csrf=tok");
    const fetch = vi.fn(async () => new Response(null, { status: 204 }));
    const api = new ApiClient({ tokens: auth, fetch });
    await api.request("/api/v1/tracks");
    await api.request("/api/v1/tags", { method: "POST", body: "{}" });
    const [getInit, postInit] = fetch.mock.calls.map((c) => (c as unknown as [string, RequestInit])[1]);
    expect(new Headers(getInit?.headers).get("X-CSRF-Token")).toBeNull();
    expect(new Headers(postInit?.headers).get("X-CSRF-Token")).toBe("tok");
    expect(new Headers(postInit?.headers).get("Authorization")).toBeNull();
    expect(postInit?.credentials).toBe("same-origin");
  });

  it("bevorzugt ein dev-Token vor der Cookie-Sitzung", async () => {
    const auth = new BrowserAuth(memoryStorage(), () => "musichub_csrf=tok");
    auth.setToken("dev-token");
    const fetch = vi.fn(async () => new Response(null, { status: 204 }));
    await new ApiClient({ tokens: auth, fetch }).request("/api/v1/tags", { method: "POST" });
    const init = (fetch.mock.calls[0] as unknown as [string, RequestInit])[1];
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer dev-token");
    expect(new Headers(init.headers).get("X-CSRF-Token")).toBeNull();
  });
});

describe("Abgelaufene Sitzung", () => {
  it("meldet 401 an onUnauthorized, nicht aber 403 oder Health-Checks", async () => {
    const seen: ApiError[] = [];
    let status = 401;
    const api = new ApiClient({
      fetch: async () =>
        json(status, { code: status === 401 ? "session_expired" : "forbidden", detail: "x" }),
      onUnauthorized: (e) => seen.push(e),
    });
    await expect(api.me()).rejects.toBeInstanceOf(ApiError);
    expect(seen.map((e) => e.problem?.code)).toEqual(["session_expired"]);
    status = 403;
    await expect(api.me()).rejects.toBeInstanceOf(ApiError);
    status = 401;
    await expect(api.authSession()).rejects.toBeInstanceOf(ApiError);
    expect(seen).toHaveLength(1);
  });
});

describe("Login", () => {
  it("baut die Login-URL mit Rückkehrpfad", () => {
    expect(loginUrl({ pathname: "/playlist/", search: "?id=1&x=2" })).toBe(
      "/api/auth/login?next=%2Fplaylist%2F%3Fid%3D1%26x%3D2",
    );
  });

  it("hat Texte für alle Fehlerkennungen des Backends", () => {
    for (const code of [
      "not_configured",
      "provider_unavailable",
      "invalid_state",
      "access_denied",
      "provider_error",
      "invalid_request",
      "token_exchange_failed",
      "invalid_id_token",
      "forbidden",
    ]) {
      expect(LOGIN_ERRORS[code]).toBeTruthy();
    }
    expect(loginErrorText("unbekannt")).toBe("Die Anmeldung ist fehlgeschlagen.");
    expect(loginErrorText(null)).toBeNull();
  });
});
