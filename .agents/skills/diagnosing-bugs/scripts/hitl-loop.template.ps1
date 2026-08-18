$ErrorActionPreference = 'Stop'

$captured = [ordered]@{}

function Step {
    param([Parameter(Mandatory)][string]$Instruction)

    Write-Host "`n>>> $Instruction"
    Read-Host '    [Enter when done]' | Out-Null
}

function Capture {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Question
    )

    Write-Host "`n>>> $Question"
    $script:captured[$Name] = Read-Host '    >'
}

Step "Open the app at http://localhost:3000 and sign in."

Capture ERRORED "Click the 'Export' button. Did it throw an error? (y/n)"
Capture ERROR_MSG "Paste the error message (or 'none'):"

Write-Host "`n--- Captured ---"
foreach ($entry in $captured.GetEnumerator()) {
    Write-Host "$($entry.Key)=$($entry.Value)"
}
