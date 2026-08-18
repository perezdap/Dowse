$ErrorActionPreference = 'Stop'

$TotalStages = 0
$script:StageIndex = 0
$EnvFile = if ($env:ENV_FILE) { $env:ENV_FILE } else { '.env' }
$script:WrittenEnv = [Collections.Generic.List[string]]::new()
$script:WrittenSecret = [Collections.Generic.List[string]]::new()
$script:Skipped = [Collections.Generic.List[string]]::new()

function banner {
    param([Parameter(Mandatory)][string]$Title)

    Clear-Host
    Write-Host "`n  $Title" -ForegroundColor Blue
    Write-Host "  $TotalStages stages`n" -ForegroundColor DarkGray
    Write-Host '  You drive the browser; this wizard captures the values you copy back.'
    pause 'Ready to start?'
}

function stage {
    param([Parameter(Mandatory)][string]$Name)

    Clear-Host
    $script:StageIndex++
    Write-Host "`n> Stage $script:StageIndex/$TotalStages - $Name" -ForegroundColor Blue
}

function say {
    param([Parameter(Mandatory)][string]$Message)
    Write-Host "  $Message"
}

function step {
    param([Parameter(Mandatory)][string]$Instruction)
    Write-Host "  * $Instruction" -ForegroundColor Blue
}

function note {
    param([Parameter(Mandatory)][string]$Message)
    Write-Host "  $Message" -ForegroundColor DarkGray
}

function warn {
    param([Parameter(Mandatory)][string]$Message)
    Write-Warning $Message
}

function open_url {
    param([Parameter(Mandatory)][uri]$Url)

    Write-Host "  opening $Url" -ForegroundColor Green
    try {
        Start-Process $Url.AbsoluteUri
    }
    catch {
        warn "couldn't open a browser - visit it manually: $Url"
    }
}

function pause {
    param([string]$Message = 'Press Enter to continue')
    Read-Host "  $Message" | Out-Null
}

function confirm {
    param([Parameter(Mandatory)][string]$Question)
    (Read-Host "  $Question [y/N]") -match '^[Yy]$'
}

function Get-ExistingValue {
    param([Parameter(Mandatory)][string]$Key)

    if (-not (Test-Path -LiteralPath $EnvFile)) {
        return $null
    }

    $prefix = "$Key="
    $line = Get-Content -LiteralPath $EnvFile |
        Where-Object { $_.StartsWith($prefix, [StringComparison]::Ordinal) } |
        Select-Object -Last 1
    if ($null -ne $line) {
        return $line.Substring($prefix.Length)
    }
}

function ask {
    param(
        [Parameter(Mandatory)][string]$Key,
        [Parameter(Mandatory)][string]$Prompt
    )

    $current = Get-ExistingValue $Key
    $suffix = if ($current) { ' [Enter keeps current]' } else { '' }
    $inputValue = Read-Host "  $Prompt$suffix"
    if (-not $inputValue -and $current) {
        $inputValue = $current
    }
    Set-Variable -Scope Script -Name $Key -Value $inputValue
}

function ask_secret {
    param(
        [Parameter(Mandatory)][string]$Key,
        [Parameter(Mandatory)][string]$Prompt
    )

    $current = Get-ExistingValue $Key
    $suffix = if ($current) { ' [Enter keeps current]' } else { '' }
    $secureValue = Read-Host "  $Prompt$suffix" -AsSecureString
    $inputValue = ConvertFrom-SecureString $secureValue -AsPlainText
    if (-not $inputValue -and $current) {
        $inputValue = $current
    }
    Set-Variable -Scope Script -Name $Key -Value $inputValue
}

function write_env {
    param(
        [Parameter(Mandatory)][string]$Key,
        [AllowEmptyString()][string]$Value
    )

    $lines = if (Test-Path -LiteralPath $EnvFile) {
        @(Get-Content -LiteralPath $EnvFile)
    }
    else {
        @()
    }
    $prefix = "$Key="
    $lines = @($lines | Where-Object {
        -not $_.StartsWith($prefix, [StringComparison]::Ordinal)
    })
    $lines += "$Key=$Value"
    Set-Content -LiteralPath $EnvFile -Value $lines -Encoding utf8
    $script:WrittenEnv.Add($Key)
    Write-Host "  wrote $Key -> $EnvFile" -ForegroundColor Green
}

function set_secret {
    param(
        [Parameter(Mandatory)][string]$Name,
        [AllowEmptyString()][string]$Value
    )

    if (Get-Command gh -ErrorAction SilentlyContinue) {
        $Value | gh secret set $Name 2>$null
        if ($LASTEXITCODE -eq 0) {
            $script:WrittenSecret.Add($Name)
            Write-Host "  set GitHub secret $Name" -ForegroundColor Green
            return
        }
    }
    $script:Skipped.Add("GitHub secret $Name (set it manually: gh secret set $Name)")
    warn "skipped GitHub secret $Name - gh not ready"
}

function set_var {
    param(
        [Parameter(Mandatory)][string]$Name,
        [AllowEmptyString()][string]$Value
    )

    if (Get-Command gh -ErrorAction SilentlyContinue) {
        gh variable set $Name --body $Value 2>$null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "  set GitHub variable $Name" -ForegroundColor Green
            return
        }
    }
    $script:Skipped.Add("GitHub variable $Name")
    warn "skipped GitHub variable $Name - gh not ready"
}

function finish {
    Clear-Host
    Write-Host "`n  Setup complete" -ForegroundColor Green
    if ($script:WrittenEnv.Count) {
        note "wrote $($script:WrittenEnv.Count) value(s) to ${EnvFile}: $($script:WrittenEnv -join ', ')"
    }
    if ($script:WrittenSecret.Count) {
        note "set $($script:WrittenSecret.Count) GitHub secret(s): $($script:WrittenSecret -join ', ')"
    }
    if ($script:Skipped.Count) {
        warn 'still to do by hand:'
        foreach ($item in $script:Skipped) {
            note "  - $item"
        }
    }
    Write-Host
}

# STAGES: replace this example while preserving the library above.
$TotalStages = 1

banner 'Stripe setup'

stage 'Stripe - API keys'
say "We'll grab your Stripe test keys and store them for local dev + CI."
open_url 'https://dashboard.stripe.com/test/apikeys'
step 'On the API keys page, copy the Publishable key (starts pk_test_).'
ask STRIPE_PUBLISHABLE_KEY 'Paste the publishable key:'
step "Click 'Reveal test key' on the Secret key row, then copy it."
ask_secret STRIPE_SECRET_KEY 'Paste the secret key:'
write_env STRIPE_PUBLISHABLE_KEY $STRIPE_PUBLISHABLE_KEY
write_env STRIPE_SECRET_KEY $STRIPE_SECRET_KEY
set_secret STRIPE_SECRET_KEY $STRIPE_SECRET_KEY

finish
