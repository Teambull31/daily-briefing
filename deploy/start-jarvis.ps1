# Démarrage de Jarvis sous Windows (relance automatique en cas de plantage).
# Pour le lancer à l'ouverture de session : Planificateur de tâches > Créer une tâche >
#   Déclencheur "À l'ouverture de session", Action :
#   powershell -WindowStyle Hidden -ExecutionPolicy Bypass -File C:\chemin\daily-briefing\deploy\start-jarvis.ps1
Set-Location (Join-Path $PSScriptRoot "..")
while ($true) {
    & .\.venv\Scripts\python.exe -m jarvis
    Write-Host "Jarvis s'est arrêté, redémarrage dans 10 s..."
    Start-Sleep -Seconds 10
}
