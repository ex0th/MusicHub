"""Import und Export der Bibliothek.

Aufbau:
* ``decoding``  – Upload prüfen (Größe, Typ, Kodierung, Binärinhalt) und als Text liefern
* ``records``   – neutrales Zwischenformat ``ImportRecord``/``ParseResult`` und Feldparser
* ``formats``   – ein Adapter pro Dateiformat; ``detect_and_parse`` wählt den passenden
* ``matching``  – Abgleich mit der Bibliothek (rein, ohne I/O)
* ``service``   – Importlauf: analysieren, Entscheidungen, transaktionaler Commit, Bericht
* ``export``    – Bibliothek/Playlists als MusicHub-CSV/-JSON, CSV gegen Formula-Injection gesichert
"""
