-- Portable starter: run this file through Scripting > Run Script in 3ds Max.
-- Resolves only its own folder; installs no macros, menus, callbacks or services.
(
    local startupError = undefined
    local importerFile = undefined
    try
    (
        local importerFolder = getFilenamePath (getSourceFileName())
        importerFile = importerFolder + "tencent_fbx_importer_downscale.py"
        if doesFileExist importerFile then
        (
            -- Handle every Python error without Max opening the script editor.
            local result = python.ExecuteFile importerFile throwOnError:false clearUndoBuffer:false
            if result != #success do
                startupError = "Python-Startfehler (" + (result as string) + "):\n" + (python.GetLastError())
        )
        else
            startupError = "Importer-Dateien fehlen. Bitte das gesamte ZIP entpacken und alle Dateien zusammenlassen."
    )
    catch
        startupError = "MAXScript-Startfehler:\n" + (getCurrentException() as string)

    if startupError != undefined do
    (
        local details = startupError + "\n\n3ds Max: " + ((maxVersion()) as string)
        if importerFile != undefined do details += "\nDatei: " + importerFile
        format "Tencent Importer - Start fehlgeschlagen:\n%\n" details
        messageBox (details + "\n\nDiese Meldung steht auch im Scripting Listener (F11).") title:"Tencent Importer - Startfehler"
    )
)
