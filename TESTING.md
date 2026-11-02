# Tests

Die Tests laufen in normalem Python außerhalb von 3ds Max. Sie erzeugen synthetische FBX-/PNG-Daten und temporäre Dateien. Einige Windows-Tests nutzen OLE-Dateieigenschaften und mehrere Python-Prozesse für Dateisperren; sie starten keine Max-Sitzung.

## Ausführen

Aus dem Repositoryordner:

```powershell
python -m pip install -r requirements-dev.txt
python -B -m unittest discover -s tests -p "test_*.py" -v
```

`requirements-dev.txt` hält die Bildbibliotheken der geprüften Entwicklungsumgebung fest. Diese Installation ist für Tests in einer eigenen Python-Umgebung gedacht und ändert nicht das Python von 3ds Max.

## Geprüfter Stand

Am 02.11.2026 bestand die für dieses Repository vorbereitete Fassung unter Windows 64 Bit mit Python 3.11.9 den vollständigen Lauf:

- 204 Tests entdeckt.
- 203 bestanden.
- 1 übersprungen: Das Erstellen eines symbolischen Links erforderte auf dem Testsystem zusätzliche Windows-Rechte.
- Keine fehlgeschlagenen Tests.

Abgedeckt werden unter anderem FBX-Material-/Texturzuordnung, Dateinamen und Asset-Identität, Vorprüfung, bestätigtes Ersetzen, Wiederherstellung bei Schreibfehlern, konkurrierende Dateisperren, Mesh-Vorschau-Daten, Layerhistorie und Umgang mit alten Importer-Metadaten.

Der Test zur Trennung zweier FBX-Quellen verwendet zwei erzeugte Dateien mit identischem eingebettetem Texturnamen und unterschiedlichen PNG-Inhalten. Er kontrolliert getrennte Ausgabepfade und unveränderte Nutzdaten; reale Modell-Dateien werden nicht mitgeliefert.

## Grenzen

Die Tests ersetzen keine Prüfung von Oberfläche, Import, tyOptimize oder Rendering innerhalb von 3ds Max. Diese native Prüfung wurde für die Veröffentlichung nicht erneut ausgeführt. Ebenso fehlt eine Abnahme auf einem konkreten SMB-/NAS-System.

Windows-spezifische Tests werden auf anderen Betriebssystemen übersprungen. `tests/metadata_test_support.py` erzeugt ausschließlich synthetische Testdaten und wird vom Importer nicht geladen.

