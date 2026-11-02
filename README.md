# TencentToVray

Portabler FBX-Batch-Importer **2.06** für **3ds Max 2024 unter Windows**. Er importiert Tencent-/Hunyuan-FBX-Dateien, verarbeitet eingebettete Texturen, erstellt V-Ray-Materialien und speichert jedes Asset mit seinen Maps getrennt ab.

## Voraussetzungen

- 3ds Max 2024, 64 Bit.
- V-Ray mit den Klassen `VRayMtl` und `VRayBitmap`, wenn **Texturen** aktiviert ist.
- tyOptimize, wenn **Optimizer** aktiviert ist.
- Für den normalen Start wird das Python/Qt der Max-Installation verwendet.

Andere Max-Hauptversionen und abweichende Plugin-Versionen sind nicht pauschal geprüft. Modelle, Texturen, 3ds Max, V-Ray und tyOptimize werden nicht mitgeliefert.

## Start und Bedienung

1. Dieses Repository vollständig herunterladen und lokal entpacken. Alle `tencent_*.py` neben `START_Importer.ms` zusammenlassen.
2. In Max **Scripting → Run Script → START_Importer.ms** ausführen.
3. **FBX hinzufügen**, Vorschaubilder prüfen und für jede Zeile einen Asset-Namen vergeben.
4. Zielordner und Layer wählen; Texturen, Optimizer, Restanteil und Map-Auflösung einstellen.
5. **Vorprüfen** ausführen, Fehler beheben und vorhandene Ziele gegebenenfalls zum Ersetzen bestätigen.
6. **Importieren** starten.

Ein erneuter Start zeigt das bereits geöffnete Importer-Fenster. Für ein Update dieses Fenster zuerst schließen. **Stoppen** wirkt zwischen Assets; laufende native Max-Berechnungen werden nicht gewaltsam abgebrochen.

Der Starter richtet keine Menüs oder Autostarts ein. Er löst seine Programmdateien relativ zum eigenen Ordner auf.

## Ausgabe und Wiederverwendung

Je Asset entstehen eine MAX-Datei und – bei eingeschalteten Texturen – ein zugehöriger Map-Ordner. Es werden keine neuen Verwaltungsmanifeste oder Importer-Eigenschaften in die Ausgabe geschrieben. Eindeutige Datei- und Mapnamen dienen zur Zuordnung; diese Struktur für späteres Relinken und Ersetzen beibehalten.

Die MAX-Ausgabe enthält die importierten Asset-Objekte. Die Texturverarbeitung verwendet eingebettete FBX-Maps; externe Texturordner werden nicht automatisch durchsucht. Mesh-Vorschauen dienen der Wiedererkennung, nicht der Prüfung von Skinning oder Animation.

Letzte Layernamen werden als persönliche Einstellung unter `%LOCALAPPDATA%/TencentToVray/layers.json` gespeichert. Diese Einstellungsdatei ist nicht Bestandteil des Repositories.

## Grenzen

- Vorprüfung und Import kontrollieren Quellen und Ziele erneut. Änderungen nach einer Freigabe können eine neue Vorprüfung erforderlich machen.
- Bestätigtes Ersetzen verwendet Sicherungen und eine Betriebssystem-Dateisperre. Bei unklarem Speicherzustand bleiben temporäre Sicherungen und Wiederherstellungsinformationen erhalten; diese vor einer Bereinigung prüfen.
- Mehrere Dateiänderungen sind bei Strom- oder Netzverlust nicht vollständig atomar. Der konkrete SMB-/NAS-Betrieb muss mit den eigenen Berechtigungen und mehreren Arbeitsplätzen geprüft werden.
- Manuell umbenannte Objekte lassen sich ohne zusätzliche Verwaltungsdaten nicht immer automatisch wiedererkennen.
- Beim Import werden keine Dienste, Lizenzkonfigurationen oder globalen Umgebungseinstellungen geändert.

## Entwicklung und Tests

Die Python-Module, der portable Starter und isolierte Unit-Tests sind enthalten. Einstieg für Entwickler: `tencent_importer_core.py`; Batch-Oberfläche: `tencent_batch_ui.py`.

Testbefehl und Umfang stehen in [TESTING.md](TESTING.md). Die aktuelle Veröffentlichungsprüfung startete keine Max-Sitzung; bestandene Python-Tests ersetzen keinen Import-/Render-Test in Max.

## Lizenzstatus und Fremdsoftware

In den ursprünglichen Projektquellen liegt keine eigenständige Softwarelizenz vor. Für diese Veröffentlichung wurde keine neue Open-Source-Lizenz hinzugefügt. Die Namen und Rechte der erwähnten Drittanbieter bleiben bei ihren jeweiligen Inhabern; deren Software und Assets sind nicht Teil dieses Repositories.

Die FBX-Transformationsberechnung enthält einen Quellenverweis auf die Autodesk-FBX-Dokumentation in `tencent_mesh_preview.py`.

