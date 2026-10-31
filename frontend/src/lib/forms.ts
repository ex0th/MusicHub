import { formatDuration, parseDuration } from "./api";
import type { TrackDetail, TrackInput } from "./types";

const text = (data: FormData, name: string): string => String(data.get(name) ?? "").trim();
const optional = (data: FormData, name: string): string | null => text(data, name) || null;

/** Formularwerte -> API-Eingabe. Wirft bei ungültiger Dauer eine lesbare Fehlermeldung. */
export function readTrackForm(form: HTMLFormElement): TrackInput {
  const data = new FormData(form);
  const input: TrackInput = {
    title: text(data, "title"),
    artist: text(data, "artist"),
    version: optional(data, "version"),
    album: optional(data, "album"),
    duration_ms: parseDuration(text(data, "duration")),
    release_date: optional(data, "release_date"),
    isrc: optional(data, "isrc"),
  };
  if (form.elements.namedItem("notes")) input.notes = optional(data, "notes");
  return input;
}

export function readLinks(form: HTMLFormElement): string[] {
  const raw = String(new FormData(form).get("links") ?? "");
  return raw
    .split(/\s+/)
    .map((line) => line.trim())
    .filter(Boolean);
}

export function fillTrackForm(form: HTMLFormElement, track: TrackDetail): void {
  const set = (name: string, value: string | null) => {
    const el = form.elements.namedItem(name);
    if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement) el.value = value ?? "";
  };
  set("title", track.title);
  set("artist", track.artist);
  set("version", track.version);
  set("album", track.album);
  set("duration", track.duration_ms ? formatDuration(track.duration_ms) : "");
  set("release_date", track.release_date);
  set("isrc", track.isrc);
  set("notes", track.notes);
}
