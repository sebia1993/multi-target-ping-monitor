param(
    [string]$Name = "MultiPingCheck",
    [string]$Tag = "",
    [string]$Title = "",
    [string]$Notes = "",
    [switch]$SkipBuild,
    [switch]$SkipVerify,
    [switch]$SkipUpload,
    [switch]$Draft,
    [switch]$Prerelease,
    [switch]$AllowDirty
)

$ErrorActionPreference = "Stop"

# 이 스크립트는 로컬에서 검증된 Windows 실행 파일을 만든 뒤 ZIP으로 묶고,
# 선택적으로 GitHub Release까지 올리는 배포 자동화입니다.
# 평소 백업/동기화만 할 때는 Git commit/push만 쓰고, 실제 첨부 ZIP Release가 필요할 때 실행합니다.

function Require-Command {
    param(
        [Parameter(Mandatory = $true)][string]$Command,
        [Parameter(Mandatory = $true)][string]$InstallHint
    )

    if (-not (Get-Command $Command -ErrorAction SilentlyContinue)) {
        throw "$Command command was not found. $InstallHint"
    }
}

function Resolve-CommandPath {
    param(
        [Parameter(Mandatory = $true)][string]$Command,
        [string[]]$FallbackPaths = @()
    )

    $Found = Get-Command $Command -ErrorAction SilentlyContinue
    if ($Found) {
        return $Found.Source
    }
    # GitHub CLI처럼 설치되어 있어도 PATH에 없는 도구는 흔한 설치 위치를 추가로 확인합니다.
    foreach ($Path in $FallbackPaths) {
        if (Test-Path -LiteralPath $Path) {
            return $Path
        }
    }
    return ""
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed: $FilePath $($Arguments -join ' ')"
    }
}

function Get-SafeFileToken {
    param([Parameter(Mandatory = $true)][string]$Value)
    return ($Value -replace '[^A-Za-z0-9._-]', '_')
}

$Root = Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")
Set-Location -LiteralPath $Root.Path

# 배포에 필요한 기본 도구가 있는지 먼저 확인합니다.
Require-Command "git" "Install Git for Windows and retry."
Require-Command "python" "Install Python and retry."
Require-Command "cyclonedx-py" "Install the hash-locked development dependencies and retry."
$GhCommand = Resolve-CommandPath "gh" @(
    "C:\Program Files\GitHub CLI\gh.exe",
    "C:\Program Files (x86)\GitHub CLI\gh.exe",
    "$env:LOCALAPPDATA\Programs\GitHub CLI\gh.exe"
)
if (-not $SkipUpload) {
    # GitHub Release 업로드는 GitHub CLI 로그인 상태가 필요합니다.
    # 로그인하지 않은 PC에서는 -SkipUpload로 로컬 ZIP 생성까지만 수행할 수 있습니다.
    if (-not $GhCommand) {
        throw "gh command was not found. Install GitHub CLI from https://cli.github.com/ and run: gh auth login"
    }
}

# GitHub Actions의 packaging 단계는 dispatch 시점 SHA를 detached HEAD로 checkout합니다.
# 로컬 upload는 push 대상이 명확해야 하므로 계속 normal branch만 허용합니다.
$Branch = (& git rev-parse --abbrev-ref HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or -not $Branch) {
    throw "Could not determine the current Git checkout state."
}
$DetachedHead = $Branch -eq "HEAD"
if ($DetachedHead -and -not $SkipUpload) {
    throw "Release publishing requires a normal checked-out branch."
}
if ($DetachedHead) {
    $Branch = "detached"
}

if (-not $SkipUpload) {
    $Remote = (& git remote get-url origin).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $Remote) {
        throw "Git remote 'origin' was not found."
    }
}

$Status = (& git status --porcelain)
if ($Status -and -not $AllowDirty) {
    # 소스 변경사항이 섞인 상태에서 Release를 만들면 어떤 코드가 배포됐는지 추적하기 어렵습니다.
    throw "Working tree has uncommitted changes. Commit local work before publishing, or pass -AllowDirty for packaging-only checks."
}

$Version = (& python -c "from app import __version__; print(__version__)").Trim()
if ($LASTEXITCODE -ne 0 -or -not $Version) {
    throw "Could not determine the application version."
}
if (-not $Tag) {
    $Tag = "v$Version"
}
if ($Tag -ne "v$Version") {
    throw "Release tag '$Tag' does not match the single source version 'v$Version'."
}
if (-not $Title) {
    $Title = "$Name $Tag"
}

$Head = (& git rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or -not $Head) {
    throw "Could not determine current commit."
}

$PreviousTag = (& git describe --tags --abbrev=0 2>$null).Trim()
if ($LASTEXITCODE -ne 0) {
    $PreviousTag = ""
}
$ChangeRange = if ($PreviousTag) { "$PreviousTag..HEAD" } else { "HEAD" }
# 릴리즈 노트의 변경사항은 직전 태그 이후 commit 제목으로 자동 작성합니다.
# 사용자가 GitHub Release에서 어떤 작업이 들어갔는지 바로 볼 수 있게 하기 위한 단계입니다.
$ChangeLines = @(& git log --pretty=format:"- %s (%h)" --no-merges $ChangeRange)
if ($LASTEXITCODE -ne 0 -or -not $ChangeLines) {
    $ChangeLines = @("- 변경 커밋을 자동으로 찾지 못했습니다. GitHub의 커밋 목록을 확인하세요.")
}
$ChangeSummaryText = ($ChangeLines -join [Environment]::NewLine)

if (-not $SkipUpload) {
    $HasGhToken = -not [string]::IsNullOrWhiteSpace($env:GH_TOKEN) -or -not [string]::IsNullOrWhiteSpace($env:GITHUB_TOKEN)
    if ([string]::IsNullOrWhiteSpace($env:GH_TOKEN) -and -not [string]::IsNullOrWhiteSpace($env:GITHUB_TOKEN)) {
        $env:GH_TOKEN = $env:GITHUB_TOKEN
    }
    if (-not $HasGhToken) {
        $AuthOutput = (& $GhCommand auth status 2>&1)
        if ($LASTEXITCODE -ne 0) {
            throw "GitHub CLI is not authenticated. Run: gh auth login"
        }
    }
    else {
        Write-Host "Using GitHub token from environment for release upload."
    }
    Invoke-Checked "git" @("push", "origin", $Branch)
}

if (-not $SkipVerify) {
    # 소스 검증을 먼저 실행해 빌드 전에 빠르게 실패시키고, --exe 단계의 중복 pytest를 피합니다.
    Invoke-Checked "python" @("scripts\verify_release.py")
}

if (-not $SkipBuild) {
    # PyInstaller 빌드 스크립트가 dist\<Name>\<Name>.exe를 생성합니다.
    Invoke-Checked "powershell" @(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        (Join-Path $Root.Path "build_windows_exe.ps1"),
        "-Name",
        $Name
    )
}

if (-not $SkipVerify) {
    # 실행 파일이 실제로 뜨는지, 필수 파일이 빠지지 않았는지 검증합니다.
    Invoke-Checked "python" @("scripts\verify_release.py", "--exe")
}

# 검증된 dist 폴더 전체를 ZIP으로 묶습니다. 사용자는 이 ZIP을 받아 압축을 풀고 EXE를 실행하면 됩니다.
$DistDir = Resolve-Path -LiteralPath (Join-Path $Root.Path "dist\$Name")
$ExePath = Resolve-Path -LiteralPath (Join-Path $DistDir.Path "$Name.exe")
$ReleaseDir = Join-Path $Root.Path "release"
New-Item -ItemType Directory -Force -Path $ReleaseDir | Out-Null

$ExeItem = Get-Item -LiteralPath $ExePath.Path
$PackageReadmePath = Join-Path $DistDir.Path "README-실행안내.txt"
@"
MultiPingCheck 실행 안내

1. ZIP 파일을 먼저 원하는 폴더에 압축 해제합니다.
2. 압축 해제한 폴더 안의 $($ExeItem.Name)을 실행합니다.
3. Python, PySide6, GitHub CLI는 사용자 PC에 따로 설치할 필요가 없습니다.
4. Windows SmartScreen 또는 Defender 경고가 처음 실행 시 표시될 수 있습니다. 배포 출처와 SHA256 값을 확인한 뒤 실행 여부를 결정하세요.

사용 방법

1. IPv4 주소는 한 줄에 하나씩 입력하거나 Excel의 IP 열을 그대로 붙여넣습니다.
2. 시작 버튼을 누르면 입력 영역이 접히고 대상별 실시간 그래프가 표시됩니다.
3. 그래프 색상은 초록=정상, 주황=주의, 빨강=장애, 회색=대기 또는 일시중지입니다.
4. 측정 중에도 IP 추가, 이름 지정, 개별 일시중지 또는 삭제가 가능합니다.
5. 측정을 끝낼 때 중지 버튼을 누릅니다.
6. 사내 테스트 중 수정 요청을 만들려면 F12로 개발자 모드를 열고, 화면 또는 기능을 선택한 뒤 요청문을 복사합니다.

데이터와 오류 확인

- 세션: %LOCALAPPDATA%\MultiPingCheck\session_logs
- 진단 로그: %LOCALAPPDATA%\MultiPingCheck\logs\multipingcheck.log
- APP_STARTUP_FAILED 또는 APP_UNEXPECTED_ERROR가 반복되면 진단 로그를 확인합니다.
- SESSION_LOG_WRITE_FAILED가 표시되면 디스크 공간, 파일 잠금, 폴더 권한을 확인하고 기존 세션 파일은 삭제하지 않습니다.
- SESSION_LOG_CORRUPTED가 표시되면 원본 세션을 보관합니다. 불완전한 CSV/XLSX 내보내기는 자동으로 차단됩니다.
- 개발자 모드 요청문은 외부로 자동 전송되지 않으며, 회사 정보 마스킹은 기본 ON입니다.
- 비밀번호, 토큰, API 키와 인증정보는 마스킹 옵션과 관계없이 요청문에서 제거됩니다.

배포 정보

- 프로그램: $Name
- 태그: $Tag
- 기준 커밋: $Head
- 브랜치: $Branch
"@ | Set-Content -LiteralPath $PackageReadmePath -Encoding UTF8

$SafeTag = Get-SafeFileToken $Tag
$ZipPath = Join-Path $ReleaseDir "${Name}_${SafeTag}.zip"
if (Test-Path -LiteralPath $ZipPath) {
    Remove-Item -LiteralPath $ZipPath -Force
}
Compress-Archive -Path (Join-Path $DistDir.Path "*") -DestinationPath $ZipPath -Force

$ZipItem = Get-Item -LiteralPath $ZipPath
$ZipHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $ZipItem.FullName).Hash.ToLowerInvariant()
$ChecksumPath = "$($ZipItem.FullName).sha256"
Set-Content -LiteralPath $ChecksumPath -Value "$ZipHash  $($ZipItem.Name)" -Encoding ASCII
$ChecksumItem = Get-Item -LiteralPath $ChecksumPath

$SbomPath = Join-Path $ReleaseDir "${Name}_${SafeTag}_sbom.cdx.json"
Invoke-Checked "cyclonedx-py" @(
    "requirements",
    "requirements.lock",
    "--output-reproducible",
    "--output-format",
    "JSON",
    "--output-file",
    $SbomPath
)
$SbomItem = Get-Item -LiteralPath $SbomPath
$SbomPayload = Get-Content -LiteralPath $SbomItem.FullName -Raw | ConvertFrom-Json
if ($SbomPayload.bomFormat -ne "CycloneDX" -or $SbomPayload.specVersion -ne "1.6" -or @($SbomPayload.components).Count -lt 1) {
    throw "Generated SBOM failed the CycloneDX 1.6 validation gate."
}
$SbomHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $SbomItem.FullName).Hash.ToLowerInvariant()

$ManifestPath = Join-Path $ReleaseDir "${Name}_${SafeTag}_release-manifest.json"
[ordered]@{
    schema_version = 1
    application_version = $Version
    tag = $Tag
    source_commit = $Head
    zip = [ordered]@{
        name = $ZipItem.Name
        bytes = $ZipItem.Length
        sha256 = $ZipHash
    }
    sbom = [ordered]@{
        name = $SbomItem.Name
        bytes = $SbomItem.Length
        sha256 = $SbomHash
        format = "CycloneDX 1.6 JSON"
    }
} | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $ManifestPath -Encoding UTF8
$ManifestItem = Get-Item -LiteralPath $ManifestPath

Invoke-Checked "python" @(
    "scripts\verify_release_assets.py",
    "--zip", $ZipItem.FullName,
    "--checksum", $ChecksumItem.FullName,
    "--sbom", $SbomItem.FullName,
    "--manifest", $ManifestItem.FullName,
    "--expected-version", $Version,
    "--expected-source-commit", $Head
)

if ($SkipUpload) {
    Write-Host "Release package created without upload."
    Write-Host "EXE: $($ExeItem.FullName) ($($ExeItem.Length) bytes)"
    Write-Host "ZIP: $($ZipItem.FullName) ($($ZipItem.Length) bytes)"
    Write-Host "SHA256: $ZipHash"
    Write-Host "CHECKSUM: $($ChecksumItem.FullName)"
    Write-Host "SBOM: $($SbomItem.FullName) ($($SbomItem.Length) bytes; SHA256 $SbomHash)"
    Write-Host "MANIFEST: $($ManifestItem.FullName)"
    exit 0
}

# 여기부터는 GitHub Release에 ZIP 파일을 첨부하는 단계입니다.
# Git tag는 "이 ZIP이 어떤 소스 커밋에서 나왔는지" 표시하는 기준점입니다.
# 나중에 문제가 생기면 태그를 보고 같은 소스 상태를 다시 찾을 수 있습니다.
& git rev-parse -q --verify "refs/tags/$Tag" *> $null
if ($LASTEXITCODE -eq 0) {
    throw "Git tag already exists: $Tag"
}

Invoke-Checked "git" @("tag", "-a", $Tag, "-m", $Title)
Invoke-Checked "git" @("push", "origin", $Tag)

$NotesPath = Join-Path $env:TEMP "${Name}_${SafeTag}_release_notes.md"
$NotesText = $Notes.Trim()
$OptionalNotesSection = if ($NotesText) {
    @"
## 릴리즈 메모

$NotesText

"@
}
else {
    ""
}

@"
$OptionalNotesSection
## 변경사항

$ChangeSummaryText

## 실행 파일

- 실행 파일: $($ExeItem.Name) ($($ExeItem.Length) bytes)
- 압축 파일: $($ZipItem.Name) ($($ZipItem.Length) bytes)
- 압축 해제 안내: README-실행안내.txt
- ZIP SHA256: $ZipHash
- SBOM: $($SbomItem.Name) ($($SbomItem.Length) bytes)
- SBOM SHA256: $SbomHash
- 릴리스 manifest: $($ManifestItem.Name)

## 검증

- 검증 명령: scripts\verify_release.py --exe
- 기준 커밋 SHA: $Head
- 브랜치: $Branch
"@ | Set-Content -LiteralPath $NotesPath -Encoding UTF8

$ReleaseArgs = @(
    "release",
    "create",
    $Tag,
    $ZipItem.FullName,
    $ChecksumItem.FullName,
    $SbomItem.FullName,
    $ManifestItem.FullName,
    "--title",
    $Title,
    "--notes-file",
    $NotesPath
)
if ($Draft) {
    $ReleaseArgs += "--draft"
}
if ($Prerelease) {
    $ReleaseArgs += "--prerelease"
}

Invoke-Checked $GhCommand $ReleaseArgs

Write-Host "GitHub Release published: $Tag"
Write-Host "EXE: $($ExeItem.FullName) ($($ExeItem.Length) bytes)"
Write-Host "ZIP: $($ZipItem.FullName) ($($ZipItem.Length) bytes)"
Write-Host "SHA256: $ZipHash"
Write-Host "CHECKSUM: $($ChecksumItem.FullName)"
Write-Host "SBOM: $($SbomItem.FullName) ($($SbomItem.Length) bytes; SHA256 $SbomHash)"
Write-Host "MANIFEST: $($ManifestItem.FullName)"
