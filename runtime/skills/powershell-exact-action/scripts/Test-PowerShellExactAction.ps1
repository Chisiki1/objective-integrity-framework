[CmdletBinding(DefaultParameterSetName = 'Command')]
param(
    [Parameter(Mandatory = $true, ParameterSetName = 'Command')]
    [string]$CommandText,

    [Parameter(Mandatory = $true, ParameterSetName = 'Json')]
    [string]$InputJsonPath,

    [switch]$IncludeSource
)

$ErrorActionPreference = 'Stop'

function Get-TextSha256 {
    param([Parameter(Mandatory = $true)][string]$Text)
    $bytes = [System.Text.UTF8Encoding]::new($false).GetBytes($Text)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '')
    }
    finally {
        $sha.Dispose()
    }
}

function Test-ForeachDirectPipe {
    param(
        [Parameter(Mandatory = $true)][System.Management.Automation.Language.Token[]]$Tokens
    )
    $hits = [System.Collections.Generic.List[object]]::new()
    for ($i = 0; $i -lt $Tokens.Count; $i++) {
        if ($Tokens[$i].Kind.ToString() -ne 'Foreach') { continue }
        $openIndex = -1
        for ($j = $i + 1; $j -lt $Tokens.Count; $j++) {
            if ($Tokens[$j].Kind.ToString() -eq 'LCurly') { $openIndex = $j; break }
        }
        if ($openIndex -lt 0) { continue }
        $depth = 0
        $closeIndex = -1
        for ($j = $openIndex; $j -lt $Tokens.Count; $j++) {
            $kind = $Tokens[$j].Kind.ToString()
            if ($kind -eq 'LCurly') { $depth++ }
            if ($kind -eq 'RCurly') {
                $depth--
                if ($depth -eq 0) { $closeIndex = $j; break }
            }
        }
        if ($closeIndex -lt 0) { continue }
        $nextIndex = $closeIndex + 1
        while ($nextIndex -lt $Tokens.Count -and $Tokens[$nextIndex].Kind.ToString() -in @('NewLine', 'Comment')) {
            $nextIndex++
        }
        if ($nextIndex -lt $Tokens.Count -and $Tokens[$nextIndex].Kind.ToString() -eq 'Pipe') {
            $hits.Add([pscustomobject]@{
                constraint_id = 'PS-FOREACH-PIPE-001'
                repeat_family = 'POWERSHELL::FOREACH-PIPE-PARSER'
                start_offset = $Tokens[$i].Extent.StartOffset
                pipe_offset = $Tokens[$nextIndex].Extent.StartOffset
                reason = 'foreach statement block is followed directly by a pipeline token'
            })
        }
    }
    return @($hits)
}

function Test-LiteralDollarExpansion {
    param(
        [Parameter(Mandatory = $true)][System.Management.Automation.Language.Token[]]$Tokens,
        [Parameter(Mandatory = $true)][object[]]$LiteralTokens
    )
    $hits = [System.Collections.Generic.List[object]]::new()
    foreach ($token in $Tokens) {
        if ($token.Kind.ToString() -notin @('StringExpandable', 'HereStringExpandable')) { continue }
        foreach ($literal in $LiteralTokens) {
            $value = [string]$literal
            if ($value -and $token.Text.Contains($value)) {
                $hits.Add([pscustomobject]@{
                    constraint_id = 'PS-LITERAL-DOLLAR-001'
                    repeat_family = 'POWERSHELL::LITERAL-DOLLAR-ARG-EXPANSION'
                    start_offset = $token.Extent.StartOffset
                    literal_token = $value
                    reason = 'required literal dollar token appears in an interpolation-capable PowerShell string'
                })
            }
        }
    }
    return @($hits)
}

function Test-Member {
    param([Parameter(Mandatory = $true)]$Member)
    $id = if ($Member.id) { [string]$Member.id } else { 'member-1' }
    $command = [string]$Member.command
    $tokens = $null
    $parseErrors = $null
    $null = [System.Management.Automation.Language.Parser]::ParseInput($command, [ref]$tokens, [ref]$parseErrors)
    $applicable = [System.Collections.Generic.List[string]]::new()
    $applicable.Add('PS-FOREACH-PIPE-001')
    $findings = [System.Collections.Generic.List[object]]::new()
    foreach ($finding in @(Test-ForeachDirectPipe -Tokens $tokens)) { $findings.Add($finding) }

    if ($Member.literal_dollar_required -eq $true) {
        $applicable.Add('PS-LITERAL-DOLLAR-001')
        $literalTokens = @($Member.literal_tokens)
        if ($literalTokens.Count -eq 0) {
            $findings.Add([pscustomobject]@{
                constraint_id = 'PS-LITERAL-DOLLAR-001'
                repeat_family = 'POWERSHELL::LITERAL-DOLLAR-ARG-EXPANSION'
                reason = 'literal_dollar_required was true but literal_tokens was empty'
            })
        }
        else {
            foreach ($finding in @(Test-LiteralDollarExpansion -Tokens $tokens -LiteralTokens $literalTokens)) { $findings.Add($finding) }
        }
    }

    $decision = if ($findings.Count -gt 0) { 'BLOCK' } elseif ($parseErrors.Count -gt 0) { 'ERROR' } else { 'PASS' }
    $result = [ordered]@{
        id = $id
        command_sha256 = Get-TextSha256 -Text $command
        applicable_constraint_ids = @($applicable | Sort-Object -Unique)
        decision = $decision
        findings = @($findings)
        parse_errors = @($parseErrors | ForEach-Object {
            [pscustomobject]@{
                error_id = $_.ErrorId
                message = $_.Message
                start_offset = $_.Extent.StartOffset
            }
        })
        proof_ceiling = 'known mechanical PowerShell discriminator over this exact member only'
    }
    if ($IncludeSource) { $result.command = $command }
    return [pscustomobject]$result
}

try {
    if ($PSCmdlet.ParameterSetName -eq 'Json') {
        $resolvedInput = (Resolve-Path -LiteralPath $InputJsonPath).Path
        $inputObject = Get-Content -LiteralPath $resolvedInput -Raw -Encoding UTF8 | ConvertFrom-Json
        $members = @($inputObject.members)
        if ($members.Count -eq 0) { throw 'Input JSON must contain at least one members item.' }
    }
    else {
        $members = @([pscustomobject]@{ id = 'member-1'; command = $CommandText })
    }

    $results = @($members | ForEach-Object { Test-Member -Member $_ })
    $overall = if (@($results | Where-Object decision -eq 'BLOCK').Count -gt 0) {
        'BLOCK'
    }
    elseif (@($results | Where-Object decision -eq 'ERROR').Count -gt 0) {
        'ERROR'
    }
    else {
        'PASS'
    }
    $receipt = [ordered]@{
        schema_version = 'powershell-exact-action-v1'
        decision = $overall
        members = $results
        prevented_claim_eligible = $false
        note = 'A later effect record may use prevented only if this BLOCK was observed before submission and the rejected candidate was preserved.'
        proof_ceiling = 'member-level known mechanical constraint screening; authorization, semantics, safety and consumer outcome unproven'
    }
    $receipt | ConvertTo-Json -Depth 12
    if ($overall -eq 'BLOCK') { exit 2 }
    if ($overall -eq 'ERROR') { exit 1 }
    exit 0
}
catch {
    [ordered]@{
        schema_version = 'powershell-exact-action-v1'
        decision = 'ERROR'
        error = $_.Exception.Message
        proof_ceiling = 'preflight route failure only'
    } | ConvertTo-Json -Depth 6
    exit 1
}
