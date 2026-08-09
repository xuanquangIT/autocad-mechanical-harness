[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('net8.0-windows', 'net10.0-windows')]
    [string]$TargetFramework,

    [Parameter(Mandatory)]
    [ValidatePattern('^[0-9]+\.[0-9]+\.[0-9]+$')]
    [string]$AutoCADManagedApiVersion,

    [Parameter(Mandatory)]
    [ValidateSet('R25.0', 'R26.0')]
    [string]$AutoCADSeries,

    [Parameter(Mandatory)]
    [ValidatePattern('^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$')]
    [string]$PackageVersion,

    [Parameter(Mandatory)]
    [Guid]$ProductCode,

    [switch]$DevelopmentUnsigned,

    [ValidateLength(3, 254)]
    [string]$OrganizationSupportEmail,

    [ValidatePattern('^[0-9A-Fa-f]{40,128}$')]
    [string]$SigningCertificateThumbprint,

    [ValidateLength(12, 2048)]
    [string]$TimestampServer
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$bridgeRoot = $PSScriptRoot
$pluginRoot = Join-Path $bridgeRoot 'CadBridge.Plugin'
$pluginProject = Join-Path $pluginRoot 'CadBridge.Plugin.csproj'
$nugetConfig = Join-Path $bridgeRoot 'NuGet.Config'
$bundleTemplate = Join-Path $bridgeRoot 'AutoCADHarness.bundle'
$manifestTemplate = Join-Path $bundleTemplate 'PackageContents.xml'
$packageRoot = Join-Path $pluginRoot 'bin\BridgePackages'

function Assert-FileExists {
    param([Parameter(Mandatory)][string]$LiteralPath)

    if (-not (Test-Path -LiteralPath $LiteralPath -PathType Leaf)) {
        throw "Required packaging input is missing: $([IO.Path]::GetFileName($LiteralPath))"
    }
}

function Get-DotNetExecutable {
    if ($TargetFramework -notmatch '^net(?<Major>[0-9]+)\.') {
        throw "Target framework '$TargetFramework' does not identify a .NET SDK major version."
    }

    $requiredSdkMajor = [int]$Matches.Major
    $candidates = [Collections.Generic.List[string]]::new()
    $userProfile = [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
    $bundledSdk = Join-Path $userProfile ".dotnet-sdk-$requiredSdkMajor\dotnet.exe"
    if (Test-Path -LiteralPath $bundledSdk -PathType Leaf) {
        $candidates.Add($bundledSdk)
    }

    $command = Get-Command 'dotnet' -CommandType Application -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        $candidates.Add($command.Source)
    }

    foreach ($candidate in $candidates | Select-Object -Unique) {
        $sdks = & $candidate --list-sdks 2>$null
        $matchingSdks = @($sdks | Where-Object { $_ -match "^\s*$requiredSdkMajor\." })
        if ($LASTEXITCODE -eq 0 -and $matchingSdks.Count -gt 0) {
            return $candidate
        }
    }

    throw "A .NET $requiredSdkMajor SDK executable could not be located."
}

function Assert-TargetCompatibility {
    $supportedTargets = @{
        'R25.0' = @{ Framework = 'net8.0-windows'; ApiPrefix = '25.0.' }
        'R26.0' = @{ Framework = 'net10.0-windows'; ApiPrefix = '26.0.' }
    }
    $expected = $supportedTargets[$AutoCADSeries]

    if ($null -eq $expected -or $TargetFramework -ne $expected.Framework) {
        throw "Target framework '$TargetFramework' is incompatible with '$AutoCADSeries'."
    }

    if (-not $AutoCADManagedApiVersion.StartsWith($expected.ApiPrefix, [StringComparison]::Ordinal)) {
        throw "AutoCAD managed API '$AutoCADManagedApiVersion' is incompatible with '$AutoCADSeries'."
    }
}

function Assert-ReleaseInputs {
    if ($DevelopmentUnsigned) {
        if ($OrganizationSupportEmail -or $SigningCertificateThumbprint -or $TimestampServer) {
            throw 'DevelopmentUnsigned cannot be combined with release identity, signing, or timestamp inputs.'
        }

        return $null
    }

    if ([string]::IsNullOrWhiteSpace($OrganizationSupportEmail) -or
        [string]::IsNullOrWhiteSpace($SigningCertificateThumbprint) -or
        [string]::IsNullOrWhiteSpace($TimestampServer)) {
        throw 'Release packaging requires support email, signing certificate, and timestamp server.'
    }

    try {
        $address = [Net.Mail.MailAddress]::new($OrganizationSupportEmail)
    }
    catch {
        throw 'OrganizationSupportEmail must be a valid email address.'
    }

    $hostName = $address.Host.ToLowerInvariant()
    if ($address.Address -ne $OrganizationSupportEmail -or
        $hostName.EndsWith('.local', [StringComparison]::Ordinal) -or
        $hostName -eq 'localhost' -or
        -not $hostName.Contains('.')) {
        throw 'Release support email must use a routable organization domain and cannot end in .local.'
    }

    $thumbprint = $SigningCertificateThumbprint.ToUpperInvariant()
    $certificate = Get-Item -LiteralPath "Cert:\CurrentUser\My\$thumbprint" -ErrorAction SilentlyContinue
    if ($null -eq $certificate) {
        $certificate = Get-Item -LiteralPath "Cert:\LocalMachine\My\$thumbprint" -ErrorAction SilentlyContinue
    }

    if ($null -eq $certificate -or -not $certificate.HasPrivateKey -or
        $certificate.NotBefore -gt [DateTime]::UtcNow -or $certificate.NotAfter -le [DateTime]::UtcNow -or
        -not ($certificate.EnhancedKeyUsageList.ObjectId.Value -contains '1.3.6.1.5.5.7.3.3')) {
        throw 'Signing certificate must be current, contain a private key, and allow code signing.'
    }

    $timestampUri = $null
    if (-not [Uri]::TryCreate($TimestampServer, [UriKind]::Absolute, [ref]$timestampUri) -or
        $timestampUri.Scheme -ne 'https') {
        throw 'TimestampServer must be an absolute HTTPS URL from the approved signing provider.'
    }

    return $certificate
}

function Invoke-DotNet {
    param(
        [Parameter(Mandatory)][string]$Executable,
        [Parameter(Mandatory)][string[]]$Arguments
    )

    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "dotnet failed with exit code $LASTEXITCODE."
    }
}

function Assert-SafePackageTarget {
    param([Parameter(Mandatory)][string]$Target)

    $root = [IO.Path]::GetFullPath($packageRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)
    $resolved = [IO.Path]::GetFullPath($Target).TrimEnd([IO.Path]::DirectorySeparatorChar)
    if ($resolved -eq $root -or
        -not $resolved.StartsWith("$root$([IO.Path]::DirectorySeparatorChar)", [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Resolved package target escaped the fixed bridge package root.'
    }
}

function Write-ManifestXml {
    param(
        [Parameter(Mandatory)][xml]$Document,
        [Parameter(Mandatory)][string]$LiteralPath
    )

    $settings = [Xml.XmlWriterSettings]::new()
    $settings.Encoding = [Text.UTF8Encoding]::new($false)
    $settings.Indent = $true
    $settings.NewLineChars = "`n"
    $settings.NewLineHandling = [Xml.NewLineHandling]::Replace
    $writer = [Xml.XmlWriter]::Create($LiteralPath, $settings)
    try {
        $Document.Save($writer)
    }
    finally {
        $writer.Dispose()
    }
}

function Set-AndVerifyAuthenticodeSignature {
    param(
        [Parameter(Mandatory)][string]$LiteralPath,
        [Parameter(Mandatory)]$Certificate,
        [Parameter(Mandatory)][string]$TimestampUrl
    )

    $signature = Set-AuthenticodeSignature -LiteralPath $LiteralPath -Certificate $Certificate -HashAlgorithm SHA256 -TimestampServer $TimestampUrl
    if ($signature.Status -ne [Management.Automation.SignatureStatus]::Valid) {
        throw "Authenticode signing failed for $([IO.Path]::GetFileName($LiteralPath)): $($signature.Status)"
    }

    $verified = Get-AuthenticodeSignature -LiteralPath $LiteralPath
    if ($verified.Status -ne [Management.Automation.SignatureStatus]::Valid -or
        $verified.SignerCertificate.Thumbprint -ne $Certificate.Thumbprint -or
        $null -eq $verified.TimeStamperCertificate) {
        throw "Authenticode verification failed for $([IO.Path]::GetFileName($LiteralPath))."
    }
}

Assert-FileExists $pluginProject
Assert-FileExists $nugetConfig
Assert-FileExists $manifestTemplate
Assert-TargetCompatibility
$signingCertificate = Assert-ReleaseInputs
$dotnet = Get-DotNetExecutable

$modeLabel = if ($DevelopmentUnsigned) { 'DEVELOPMENT-UNSIGNED' } else { 'RELEASE-SIGNED' }
$safeVersion = $AutoCADManagedApiVersion.Replace('.', '-')
$safeFramework = $TargetFramework.Replace('.', '-').Replace('+', '-')
$safeSeries = $AutoCADSeries.Replace('.', '-')
$safePackageVersion = $PackageVersion.Replace('.', '-')
$artifactName = "$modeLabel-$safeSeries-$safeFramework-api-$safeVersion-v$safePackageVersion"
$artifactRoot = Join-Path $packageRoot $artifactName
$stagedBundle = Join-Path $artifactRoot 'AutoCADHarness.bundle'
$windowsContents = Join-Path $stagedBundle 'Contents\Windows'
Assert-SafePackageTarget $artifactRoot

if (Test-Path -LiteralPath $artifactRoot) {
    Remove-Item -LiteralPath $artifactRoot -Recurse -Force
}

New-Item -ItemType Directory -Path $windowsContents -Force | Out-Null

$commonProperties = @(
    "-p:CadBridgeTargetFramework=$TargetFramework",
    "-p:AutoCADManagedApiVersion=$AutoCADManagedApiVersion",
    "-p:Version=$PackageVersion",
    "-p:AssemblyVersion=$PackageVersion",
    "-p:FileVersion=$PackageVersion"
)
$restoreArguments = @(
    'restore',
    $pluginProject,
    '--configfile',
    $nugetConfig
) + $commonProperties
Invoke-DotNet $dotnet $restoreArguments
$buildArguments = @(
    'build',
    $pluginProject,
    '--configuration',
    'Release',
    '--framework',
    $TargetFramework,
    '--no-restore'
) + $commonProperties
Invoke-DotNet $dotnet $buildArguments

$buildOutput = Join-Path $pluginRoot "bin\Release\$TargetFramework"
$requiredAssemblies = @(
    'AutoCADHarness.dll',
    'CadBridge.Contracts.dll',
    'CadBridge.Execution.dll',
    'CadBridge.Hosting.dll',
    'CadBridge.Inspection.dll',
    'CadBridge.Ipc.dll',
    'CadBridge.Metadata.dll'
)
foreach ($assembly in $requiredAssemblies) {
    $source = Join-Path $buildOutput $assembly
    Assert-FileExists $source
    Copy-Item -LiteralPath $source -Destination (Join-Path $windowsContents $assembly)
}

Get-ChildItem -LiteralPath $buildOutput -Filter 'CadBridge.*.dll' -File |
    ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $windowsContents $_.Name) -Force
    }

$forbiddenRuntimeNames = @(
    'AcCoreMgd.dll',
    'AcDbMgd.dll',
    'AcMgd.dll',
    'AcCui.dll',
    'AcDx.dll',
    'AcWindows.dll',
    'AdWindows.dll'
)
$forbidden = Get-ChildItem -LiteralPath $windowsContents -Filter '*.dll' -File |
    Where-Object { $_.Name -in $forbiddenRuntimeNames -or $_.Name.StartsWith('Autodesk.', [StringComparison]::OrdinalIgnoreCase) }
if ($forbidden) {
    throw 'Autodesk runtime assemblies must never be redistributed in the bridge bundle.'
}

[xml]$packageManifest = Get-Content -LiteralPath $manifestTemplate -Raw
$packageManifest.ApplicationPackage.AppVersion = $PackageVersion
$packageManifest.ApplicationPackage.ProductCode = "{$($ProductCode.ToString().ToUpperInvariant())}"
$runtimeRequirements = @($packageManifest.ApplicationPackage.Components.RuntimeRequirements)
if ($runtimeRequirements.Count -ne 1) {
    throw 'PackageContents.xml must contain exactly one RuntimeRequirements element.'
}

$runtimeRequirements[0].SeriesMin = $AutoCADSeries
$runtimeRequirements[0].SeriesMax = $AutoCADSeries
if (
    $runtimeRequirements[0].SeriesMin -ne $AutoCADSeries -or
    $runtimeRequirements[0].SeriesMax -ne $AutoCADSeries) {
    throw "PackageContents.xml SeriesMin/SeriesMax must both equal explicit target '$AutoCADSeries'."
}

$moduleName = [string]$packageManifest.ApplicationPackage.Components.ComponentEntry.ModuleName
if ($moduleName -ne './Contents/Windows/AutoCADHarness.dll') {
    throw 'PackageContents.xml ModuleName does not match the staged plugin location.'
}

if (-not $DevelopmentUnsigned) {
    $packageManifest.ApplicationPackage.CompanyDetails.Email = $OrganizationSupportEmail
}

$stagedManifest = Join-Path $stagedBundle 'PackageContents.xml'
Write-ManifestXml $packageManifest $stagedManifest

if ($DevelopmentUnsigned) {
    $marker = Join-Path $stagedBundle 'DEVELOPMENT-UNSIGNED.txt'
    [IO.File]::WriteAllText(
        $marker,
        "DEVELOPMENT-UNSIGNED`nNot a release artifact. Do not deploy outside an isolated test workstation.`n",
        [Text.UTF8Encoding]::new($false))
}
else {
    foreach ($binary in Get-ChildItem -LiteralPath $windowsContents -Filter '*.dll' -File) {
        Set-AndVerifyAuthenticodeSignature $binary.FullName $signingCertificate $TimestampServer
    }
}

$checksumPath = Join-Path $stagedBundle 'SHA256SUMS.ps1'
$checksumFiles = Get-ChildItem -LiteralPath $stagedBundle -Recurse -File |
    Where-Object { $_.FullName -ne $checksumPath }
$relativePaths = @(
    $checksumFiles | ForEach-Object {
        [IO.Path]::GetRelativePath($stagedBundle, $_.FullName).Replace('\', '/')
    }
)
[Array]::Sort($relativePaths, [StringComparer]::Ordinal)
$checksumLines = foreach ($relativePath in $relativePaths) {
    $absolutePath = Join-Path $stagedBundle $relativePath.Replace('/', '\')
    $hash = (Get-FileHash -LiteralPath $absolutePath -Algorithm SHA256).Hash.ToLowerInvariant()
    "# SHA256 $hash *$relativePath"
}
$checksumContent = ($checksumLines -join "`n") + "`n"
[IO.File]::WriteAllText($checksumPath, $checksumContent, [Text.UTF8Encoding]::new($false))

if (-not $DevelopmentUnsigned) {
    Set-AndVerifyAuthenticodeSignature $checksumPath $signingCertificate $TimestampServer
}

$result = [pscustomobject]@{
    ArtifactKind = $modeLabel
    AutoCADSeries = $AutoCADSeries
    TargetFramework = $TargetFramework
    AutoCADManagedApiVersion = $AutoCADManagedApiVersion
    PackageVersion = $PackageVersion
    ProductCode = $ProductCode
    BundlePath = $stagedBundle
    AssemblyCount = @(Get-ChildItem -LiteralPath $windowsContents -Filter '*.dll' -File).Count
    ChecksumManifest = $checksumPath
}
$result
