"""Fachlogik der lokalen Musikbibliothek (Tracks, Quellen, Tags, Playlists, Statistik).

Die API-Routen bleiben dünn und rufen diese Funktionen auf. Funktionen hier arbeiten auf
einer übergebenen Session, committen aber nie selbst – das entscheidet der Aufrufer
(eine Anfrage = eine Transaktion).
"""
