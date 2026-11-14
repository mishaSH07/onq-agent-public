$ErrorActionPreference = 'Stop'
Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/shutdown' -Method Post -Headers @{'X-OnQ-Client'='dashboard'}
Write-Output 'Worker is shutting down. The desktop shortcut starts it again.'
