import { describe, expect, it, vi } from "vitest";

import { ApiClient, ApiError, formatDuration, parseDuration } from "./api";
import { MemoryTokenProvider } from "./auth";

type FetchArgs = [input: string, init: RequestInit];
const fetchMock = (impl: (...args: FetchArgs) => Promise<Response>) => vi.fn(impl);

function jsonResponse(status: number, body: unknown, type = "application/json"): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": type } });
}

describe("ApiClient", () => {
  it("hängt das Token an geschützte Aufrufe an, nicht an Health-Checks", async () => {
    const tokens = new MemoryTokenProvider();
    tokens.setToken("secret-token");
    const fetch = fetchMock(async () => jsonResponse(200, { status: "ok" }));
    const api = new ApiClient({ tokens, fetch });

    await api.me();
    await api.live();

    const meInit = fetch.mock.calls[0]?.[1];
    const liveInit = fetch.mock.calls[1]?.[1];
    expect(new Headers(meInit?.headers).get("Authorization")).toBe("Bearer secret-token");
    expect(new Headers(liveInit?.headers).get("Authorization")).toBeNull();
  });

  it("übersetzt Problem-Details in ApiError", async () => {
    const problem = {
      type: "x",
      title: "Unauthorized",
      status: 401,
      code: "unauthorized",
      detail: "Authentifizierung erforderlich.",
      request_id: "abc",
    };
    const api = new ApiClient({
      fetch: async (): Promise<Response> => jsonResponse(401, problem, "application/problem+json"),
    });
    const error = await api.me().catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(401);
    expect((error as ApiError).message).toBe("Authentifizierung erforderlich.");
    expect((error as ApiError).requestId).toBe("abc");
  });

  it("liefert den Body von /ready auch bei 503", async () => {
    const body = { status: "error", checks: { database: { status: "error", detail: "weg" } } };
    const api = new ApiClient({ fetch: async (): Promise<Response> => jsonResponse(503, body) });
    await expect(api.ready()).resolves.toEqual(body);
  });

  it("baut Query-Parameter für die Tracksuche", async () => {
    const fetch = fetchMock(async () =>
      jsonResponse(200, { items: [], total: 0, limit: 10, offset: 0 }),
    );
    await new ApiClient({ fetch }).listTracks({ q: "a&b", limit: 10 });
    expect(fetch.mock.calls[0]?.[0]).toBe("/api/v1/tracks?q=a%26b&limit=10");
  });

  it("sendet Dateien als multipart ohne JSON-Content-Type", async () => {
    const fetch = fetchMock(async () => jsonResponse(201, { id: "r1" }));
    await new ApiClient({ fetch }).uploadImport(new File(["title,artist\nA,B"], "x.csv"), "generic_csv");
    const init = fetch.mock.calls[0]?.[1];
    expect(new Headers(init?.headers).get("Content-Type")).toBeNull();
    expect(init?.body).toBeInstanceOf(FormData);
    expect((init?.body as FormData).get("format")).toBe("generic_csv");
  });

  it("liest den Dateinamen beim Download aus Content-Disposition", async () => {
    const api = new ApiClient({
      fetch: async (): Promise<Response> =>
        new Response("a,b", { headers: { "Content-Disposition": 'attachment; filename="export.csv"' } }),
    });
    const { filename, blob } = await api.download("/api/v1/export/library?format=csv");
    expect(filename).toBe("export.csv");
    expect(await blob.text()).toBe("a,b");
  });

  it("gibt bei 204 undefined zurück", async () => {
    const api = new ApiClient({ fetch: async (): Promise<Response> => new Response(null, { status: 204 }) });
    await expect(api.deleteTrack("x")).resolves.toBeUndefined();
  });
});

describe("Dauer", () => {
  it("formatiert Millisekunden", () => {
    expect(formatDuration(225_000)).toBe("3:45");
    expect(formatDuration(null)).toBe("–");
  });

  it("parst m:ss und Sekunden", () => {
    expect(parseDuration("3:45")).toBe(225_000);
    expect(parseDuration("90")).toBe(90_000);
    expect(parseDuration("  ")).toBeNull();
    expect(() => parseDuration("3:75")).toThrow();
    expect(() => parseDuration("abc")).toThrow();
  });
});
