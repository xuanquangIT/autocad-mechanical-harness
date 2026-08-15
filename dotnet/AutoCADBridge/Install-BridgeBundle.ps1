#requires -Version 7.2

[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('Validate', 'Install', 'Uninstall')]
    [string]$Action,

    [string]$BundlePath,

    [Parameter(Mandatory)]
    [ValidateSet('R25.0', 'R26.0')]
    [string]$ExpectedAutoCADSeries,

    [string]$InstallRoot,

    [switch]$DevelopmentUnsigned,

    [switch]$Upgrade,

    [switch]$AllowRunningAutoCADForDevelopmentTest,

    # Fault and policy injection are deliberately constrained to unsigned bundles in a
    # non-default development root. They exist only for deterministic crash/policy tests.
    [ValidateSet(
        'None',
        'InstallAfterPrepared',
        'InstallAfterPublishBeforeJournal',
        'UpgradeAfterOldRenameBeforeJournal',
        'UpgradeAfterPublishBeforeJournal',
        'UninstallAfterRenameBeforeJournal',
        'CleanupAfterOneDelete')]
    [string]$DevelopmentTestFault = 'None',

    [ValidateRange(0, 10000)]
    [int]$DevelopmentTestHoldMutexMilliseconds = 0,

    [string]$DevelopmentTestPreCommitBarrierPath,

    [string]$DevelopmentTestSignaturePolicyFixture
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$bundleName = 'AutoCADHarness.bundle'
$receiptName = 'CAD-HARNESS-INSTALL-RECEIPT.json'
$checksumName = 'SHA256SUMS.ps1'
$developmentMarkerName = 'DEVELOPMENT-UNSIGNED.txt'
$journalName = '.cad-harness-installer-journal.json'
$journalKeyName = '.cad-harness-installer-journal.key'
$transactionLockName = '.cad-harness-installer.lock'
$journalSchemaVersion = '2.0'
$stableUpgradeCode = '{FA1366B0-8CAB-42B6-B5A2-66D3EF37F0A5}'
$defaultInstallRoot = $null
$requiredAssemblies = @(
    'AutoCADHarness.dll',
    'CadBridge.Contracts.dll',
    'CadBridge.Execution.dll',
    'CadBridge.Hosting.dll',
    'CadBridge.Inspection.dll',
    'CadBridge.Ipc.dll',
    'CadBridge.Metadata.dll'
)
$forbiddenRuntimeNames = @(
    'AcCoreMgd.dll',
    'AcDbMgd.dll',
    'AcMgd.dll',
    'AcCui.dll',
    'AcDx.dll',
    'AcWindows.dll',
    'AdWindows.dll'
)

# Release certificate provisioning is an external production gate. An empty embedded
# allowlist intentionally makes every production action fail closed until release
# engineering adds one or more reviewed pins and signs this installer with an allowed
# identity. Rotation is explicit through AllowedPreviousSignerIds; arbitrary caller-
# supplied thumbprints are never accepted.
$approvedReleaseSigners = @()

if (-not ('CadHarnessInstallerNative' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
using System.Text;

public static class CadHarnessInstallerNative
{
    [StructLayout(LayoutKind.Sequential)]
    public struct BY_HANDLE_FILE_INFORMATION
    {
        public uint FileAttributes;
        public System.Runtime.InteropServices.ComTypes.FILETIME CreationTime;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastAccessTime;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastWriteTime;
        public uint VolumeSerialNumber;
        public uint FileSizeHigh;
        public uint FileSizeLow;
        public uint NumberOfLinks;
        public uint FileIndexHigh;
        public uint FileIndexLow;
    }

    [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
    private static extern int SHGetKnownFolderPath(
        [MarshalAs(UnmanagedType.LPStruct)] Guid rfid,
        uint flags,
        IntPtr token,
        out IntPtr path);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern SafeFileHandle CreateFileW(
        string fileName,
        uint desiredAccess,
        uint shareMode,
        IntPtr securityAttributes,
        uint creationDisposition,
        uint flagsAndAttributes,
        IntPtr templateFile);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern uint GetFinalPathNameByHandleW(
        SafeFileHandle file,
        StringBuilder path,
        uint pathLength,
        uint flags);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetFileInformationByHandle(
        SafeFileHandle file,
        out BY_HANDLE_FILE_INFORMATION information);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetFileInformationByHandleEx(
        SafeFileHandle file,
        int informationClass,
        IntPtr information,
        uint informationSize);

    public static string GetRoamingAppData()
    {
        Guid folderId = new Guid("3EB685DB-65F9-4CF6-A03A-E3EF65729F3D");
        IntPtr value;
        int result = SHGetKnownFolderPath(folderId, 0, IntPtr.Zero, out value);
        if (result != 0) Marshal.ThrowExceptionForHR(result);
        try { return Marshal.PtrToStringUni(value); }
        finally { Marshal.FreeCoTaskMem(value); }
    }

    public static SafeFileHandle OpenDirectoryHandle(string path)
    {
        const uint ShareReadWriteDelete = 0x00000001 | 0x00000002 | 0x00000004;
        const uint OpenExisting = 3;
        const uint BackupSemantics = 0x02000000;
        const uint OpenReparsePoint = 0x00200000;
        SafeFileHandle handle = CreateFileW(
            path, 0, ShareReadWriteDelete, IntPtr.Zero, OpenExisting,
            BackupSemantics | OpenReparsePoint, IntPtr.Zero);
        if (handle.IsInvalid)
        {
            int error = Marshal.GetLastWin32Error();
            handle.Dispose();
            throw new Win32Exception(error);
        }
        return handle;
    }

    public static SafeFileHandle OpenExclusiveTransactionLock(
        string path, bool requireExisting)
    {
        const uint GenericReadWrite = 0x80000000 | 0x40000000;
        const uint OpenExisting = 3;
        const uint OpenAlways = 4;
        const uint Normal = 0x00000080;
        const uint OpenReparsePoint = 0x00200000;
        const uint WriteThrough = 0x80000000;
        SafeFileHandle handle = CreateFileW(
            path, GenericReadWrite, 0, IntPtr.Zero,
            requireExisting ? OpenExisting : OpenAlways,
            Normal | OpenReparsePoint | WriteThrough, IntPtr.Zero);
        if (handle.IsInvalid)
        {
            int error = Marshal.GetLastWin32Error();
            handle.Dispose();
            throw new Win32Exception(error);
        }
        return handle;
    }

    public static string[] GetHandleIdentity(SafeFileHandle handle)
    {
        if (handle == null || handle.IsInvalid || handle.IsClosed)
            throw new InvalidOperationException("HANDLE_INVALID");
        StringBuilder finalPath = new StringBuilder(32768);
        uint length = GetFinalPathNameByHandleW(handle, finalPath,
            (uint)finalPath.Capacity, 0);
        if (length == 0 || length >= finalPath.Capacity)
            throw new Win32Exception(Marshal.GetLastWin32Error());
        BY_HANDLE_FILE_INFORMATION info;
        if (!GetFileInformationByHandle(handle, out info))
            throw new Win32Exception(Marshal.GetLastWin32Error());
        return new[] {
            finalPath.ToString(),
            info.VolumeSerialNumber.ToString("X8"),
            info.FileIndexHigh.ToString("X8") + info.FileIndexLow.ToString("X8"),
            info.FileAttributes.ToString("X8"),
            info.NumberOfLinks.ToString()
        };
    }

    public static bool HasAlternateDataStreams(SafeFileHandle handle)
    {
        const int FileStreamInfo = 7;
        const int ErrorMoreData = 234;
        int capacity = 65536;
        while (capacity <= 1048576)
        {
            IntPtr buffer = Marshal.AllocHGlobal(capacity);
            try
            {
                if (!GetFileInformationByHandleEx(
                    handle, FileStreamInfo, buffer, (uint)capacity))
                {
                    int error = Marshal.GetLastWin32Error();
                    if (error == ErrorMoreData)
                    {
                        capacity *= 2;
                        continue;
                    }
                    throw new Win32Exception(error);
                }
                int offset = 0;
                while (true)
                {
                    int next = Marshal.ReadInt32(buffer, offset);
                    int nameLength = Marshal.ReadInt32(buffer, offset + 4);
                    if (nameLength < 0 || (nameLength & 1) != 0 ||
                        offset + 24 + nameLength > capacity)
                        throw new InvalidOperationException("STREAM_INFO_INVALID");
                    string name = Marshal.PtrToStringUni(
                        IntPtr.Add(buffer, offset + 24), nameLength / 2) ?? "";
                    if (!String.Equals(name, "::$DATA", StringComparison.Ordinal))
                        return true;
                    if (next == 0) return false;
                    if (next < 24 || offset + next >= capacity)
                        throw new InvalidOperationException("STREAM_INFO_INVALID");
                    offset += next;
                }
            }
            finally { Marshal.FreeHGlobal(buffer); }
        }
        throw new Win32Exception(ErrorMoreData);
    }

    public static string[] GetDirectoryIdentity(string path)
    {
        using (SafeFileHandle handle = OpenDirectoryHandle(path))
        {
            return GetHandleIdentity(handle);
        }
    }
}
'@
}

function Stop-Installer {
    param([Parameter(Mandatory)][string]$Code)

    $exception = [InvalidOperationException]::new($Code)
    $exception.Data['CadHarnessErrorCode'] = $Code
    throw $exception
}

function Get-Sha256Text {
    param([Parameter(Mandatory)][string]$Value)

    $bytes = [Text.UTF8Encoding]::new($false).GetBytes($Value)
    return [Convert]::ToHexString(
        [Security.Cryptography.SHA256]::HashData($bytes)).ToLowerInvariant()
}

function Get-KnownFolderInstallRoot {
    try {
        $roaming = [CadHarnessInstallerNative]::GetRoamingAppData()
    }
    catch {
        Stop-Installer 'KNOWN_FOLDER_UNAVAILABLE'
    }
    if ([string]::IsNullOrWhiteSpace($roaming)) {
        Stop-Installer 'KNOWN_FOLDER_UNAVAILABLE'
    }
    return [IO.Path]::GetFullPath((Join-Path $roaming 'Autodesk\ApplicationPlugins'))
}

function ConvertFrom-FinalHandlePath {
    param([Parameter(Mandatory)][string]$Value)

    if ($Value.StartsWith('\\?\UNC\', [StringComparison]::OrdinalIgnoreCase)) {
        return "\\$($Value.Substring(8))"
    }
    if ($Value.StartsWith('\\?\', [StringComparison]::OrdinalIgnoreCase)) {
        return $Value.Substring(4)
    }
    return $Value
}

function Get-ExistingDirectoryIdentity {
    param(
        [Parameter(Mandatory)][string]$LiteralPath,
        [Parameter(Mandatory)][string]$ErrorCode,
        [switch]$AllowAlias
    )

    $requested = Get-FullLocalPath $LiteralPath $ErrorCode
    if (-not [IO.Directory]::Exists($requested)) {
        Stop-Installer $ErrorCode
    }
    Assert-NoReparseAncestors $requested
    try {
        [string[]]$identity = [CadHarnessInstallerNative]::GetDirectoryIdentity($requested)
        $canonical = Get-FullLocalPath (ConvertFrom-FinalHandlePath $identity[0]) $ErrorCode
    }
    catch [InvalidOperationException] {
        throw
    }
    catch {
        Stop-Installer $ErrorCode
    }
    if (-not $AllowAlias -and -not $requested.Equals(
            $canonical, [StringComparison]::OrdinalIgnoreCase)) {
        Stop-Installer 'PATH_ALIAS_NOT_ALLOWED'
    }
    return [pscustomobject]@{
        CanonicalPath = $canonical
        VolumeSerial = $identity[1]
        FileId = $identity[2]
    }
}

function Get-PotentialDirectoryIdentity {
    param(
        [Parameter(Mandatory)][string]$LiteralPath,
        [Parameter(Mandatory)][string]$ErrorCode
    )

    $requested = Get-FullLocalPath $LiteralPath $ErrorCode
    if ([IO.Directory]::Exists($requested)) {
        return Get-ExistingDirectoryIdentity $requested $ErrorCode
    }
    if ([IO.File]::Exists($requested)) {
        Stop-Installer $ErrorCode
    }

    $segments = [Collections.Generic.Stack[string]]::new()
    $current = $requested
    while (-not [IO.Directory]::Exists($current)) {
        $leaf = [IO.Path]::GetFileName($current)
        $parent = [IO.Path]::GetDirectoryName($current)
        if ([string]::IsNullOrWhiteSpace($leaf) -or
            [string]::IsNullOrWhiteSpace($parent) -or $parent -eq $current) {
            Stop-Installer $ErrorCode
        }
        $segments.Push($leaf)
        $current = $parent
    }
    $anchor = Get-ExistingDirectoryIdentity $current $ErrorCode
    $canonical = $anchor.CanonicalPath
    while ($segments.Count -gt 0) {
        $canonical = Join-Path $canonical $segments.Pop()
    }
    $canonical = Get-FullLocalPath $canonical $ErrorCode
    if (-not $requested.Equals($canonical, [StringComparison]::OrdinalIgnoreCase)) {
        Stop-Installer 'PATH_ALIAS_NOT_ALLOWED'
    }
    return [pscustomobject]@{
        CanonicalPath = $canonical
        VolumeSerial = $anchor.VolumeSerial
        FileId = $anchor.FileId
    }
}

function Test-PathEqualOrWithin {
    param(
        [Parameter(Mandatory)][string]$Candidate,
        [Parameter(Mandatory)][string]$Container
    )

    $candidatePath = $Candidate.TrimEnd([IO.Path]::DirectorySeparatorChar)
    $containerPath = $Container.TrimEnd([IO.Path]::DirectorySeparatorChar)
    return $candidatePath.Equals($containerPath, [StringComparison]::OrdinalIgnoreCase) -or
        $candidatePath.StartsWith(
            "$containerPath$([IO.Path]::DirectorySeparatorChar)",
            [StringComparison]::OrdinalIgnoreCase)
}

function Assert-NoAlternateDataStreams {
    param([Parameter(Mandatory)][string]$LiteralPath)

    if ([IO.Directory]::Exists($LiteralPath)) {
        for ($attempt = 0; $attempt -lt 20; $attempt++) {
            try {
                $streams = @(Get-Item -LiteralPath $LiteralPath -Stream * -ErrorAction Stop)
                foreach ($stream in $streams) {
                    if ([string]$stream.Stream -cne ':$DATA') {
                        Stop-Installer 'ALTERNATE_DATA_STREAM_NOT_ALLOWED'
                    }
                }
                return
            }
            catch {
                if ($_.Exception.Data.Contains('CadHarnessErrorCode')) { throw }
                if ($attempt -eq 19) { Stop-Installer 'PATH_UNREADABLE' }
            }
            [Threading.Thread]::Sleep(25)
        }
    }

    $hasAlternateStreams = $false
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        $metadataHandle = $null
        try {
            # Use a zero-access native metadata handle with read/write/delete sharing.
            # The PowerShell provider can report PATH_UNREADABLE immediately after a
            # fault-injected process exits while Windows security software still owns a
            # short-lived provider handle. The native query observes the same stream
            # metadata without weakening the ADS or identity boundary.
            $metadataHandle = [CadHarnessInstallerNative]::OpenDirectoryHandle($LiteralPath)
            $hasAlternateStreams = [CadHarnessInstallerNative]::HasAlternateDataStreams(
                $metadataHandle)
            if ($hasAlternateStreams) {
                Stop-Installer 'ALTERNATE_DATA_STREAM_NOT_ALLOWED'
            }
            return
        }
        catch {
            if ($_.Exception.Data.Contains('CadHarnessErrorCode')) { throw }
            if ($attempt -eq 19) { Stop-Installer 'PATH_UNREADABLE' }
        }
        finally {
            if ($null -ne $metadataHandle) { $metadataHandle.Dispose() }
        }
        [Threading.Thread]::Sleep(25)
    }
}

function Get-FullLocalPath {
    param(
        [Parameter(Mandatory)][string]$LiteralPath,
        [Parameter(Mandatory)][string]$ErrorCode
    )

    try {
        $fullPath = [IO.Path]::GetFullPath($LiteralPath)
        $root = [IO.Path]::GetPathRoot($fullPath)
    }
    catch {
        Stop-Installer $ErrorCode
    }

    if ([string]::IsNullOrWhiteSpace($root) -or
        $fullPath.StartsWith('\\', [StringComparison]::Ordinal) -or
        $fullPath.StartsWith('//', [StringComparison]::Ordinal) -or
        $fullPath.StartsWith('\\?\', [StringComparison]::Ordinal) -or
        $fullPath.StartsWith('\\.\', [StringComparison]::Ordinal)) {
        Stop-Installer $ErrorCode
    }

    try {
        $drive = [IO.DriveInfo]::new($root)
    }
    catch {
        Stop-Installer $ErrorCode
    }
    if ($drive.DriveType -eq [IO.DriveType]::Network) {
        Stop-Installer $ErrorCode
    }

    $normalized = $fullPath.TrimEnd([IO.Path]::DirectorySeparatorChar)
    if ($normalized -match '^[A-Za-z]:$') {
        return "$normalized$([IO.Path]::DirectorySeparatorChar)"
    }
    return $normalized
}

function Test-ReparsePoint {
    param([Parameter(Mandatory)][string]$LiteralPath)

    try {
        $attributes = [IO.File]::GetAttributes($LiteralPath)
    }
    catch {
        Stop-Installer 'PATH_UNREADABLE'
    }

    return ($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
}

function Assert-NoReparseTree {
    param([Parameter(Mandatory)][string]$LiteralPath)

    $null = Get-SafeTreeFiles $LiteralPath 'PATH_UNREADABLE'
}

function Assert-NoReparseAncestors {
    param([Parameter(Mandatory)][string]$LiteralPath)

    $current = [IO.Path]::GetFullPath($LiteralPath)
    while (-not [IO.Directory]::Exists($current) -and -not [IO.File]::Exists($current)) {
        $parent = [IO.Path]::GetDirectoryName($current)
        if ([string]::IsNullOrWhiteSpace($parent) -or $parent -eq $current) {
            Stop-Installer 'PATH_UNREADABLE'
        }
        $current = $parent
    }

    while (-not [string]::IsNullOrWhiteSpace($current)) {
        if (Test-ReparsePoint $current) {
            Stop-Installer 'REPARSE_POINT_NOT_ALLOWED'
        }
        $parent = [IO.Path]::GetDirectoryName(
            $current.TrimEnd([IO.Path]::DirectorySeparatorChar))
        if ([string]::IsNullOrWhiteSpace($parent) -or $parent -eq $current) {
            break
        }
        $current = $parent
    }
}

function Get-SafeTreeFiles {
    param(
        [Parameter(Mandatory)][string]$LiteralPath,
        [Parameter(Mandatory)][string]$ErrorCode
    )

    Assert-NoReparseAncestors $LiteralPath
    $directories = [Collections.Generic.Stack[string]]::new()
    $files = [Collections.Generic.List[IO.FileInfo]]::new()
    $directories.Push([IO.Path]::GetFullPath($LiteralPath))
    while ($directories.Count -gt 0) {
        $current = $directories.Pop()
        try {
            Assert-NoAlternateDataStreams $current
            $entries = [IO.Directory]::EnumerateFileSystemEntries($current)
            foreach ($entry in $entries) {
                $attributes = [IO.File]::GetAttributes($entry)
                if (($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                    Stop-Installer 'REPARSE_POINT_NOT_ALLOWED'
                }
                if (($attributes -band [IO.FileAttributes]::Directory) -ne 0) {
                    $directories.Push($entry)
                }
                else {
                    Assert-NoAlternateDataStreams $entry
                    $files.Add([IO.FileInfo]::new($entry))
                }
            }
        }
        catch [InvalidOperationException] {
            throw
        }
        catch {
            Stop-Installer $ErrorCode
        }
    }
    return $files.ToArray()
}

function Get-RelativeDirectoryInventory {
    param([Parameter(Mandatory)][string]$Root)

    $rootFull = [IO.Path]::GetFullPath($Root)
    $pending = [Collections.Generic.Stack[string]]::new()
    $directories = [Collections.Generic.List[string]]::new()
    $pending.Push($rootFull)
    while ($pending.Count -gt 0) {
        $current = $pending.Pop()
        Assert-NoAlternateDataStreams $current
        try {
            foreach ($entry in [IO.Directory]::EnumerateDirectories($current)) {
                if (Test-ReparsePoint $entry) {
                    Stop-Installer 'REPARSE_POINT_NOT_ALLOWED'
                }
                Assert-NoAlternateDataStreams $entry
                $relative = [IO.Path]::GetRelativePath($rootFull, $entry).Replace('\', '/')
                $null = Get-SafeChildPath $rootFull "$relative/owned-placeholder"
                $directories.Add($relative)
                $pending.Push($entry)
            }
        }
        catch [InvalidOperationException] {
            throw
        }
        catch {
            Stop-Installer 'BUNDLE_UNREADABLE'
        }
    }
    [string[]]$sorted = $directories.ToArray()
    [Array]::Sort($sorted, [StringComparer]::Ordinal)
    return $sorted
}

function Assert-ExistingDirectory {
    param(
        [Parameter(Mandatory)][string]$LiteralPath,
        [Parameter(Mandatory)][string]$ErrorCode
    )

    if (-not [IO.Directory]::Exists($LiteralPath)) {
        Stop-Installer $ErrorCode
    }
    Assert-NoReparseAncestors $LiteralPath
}

function Get-SafeChildPath {
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][string]$RelativePath
    )

    if ([string]::IsNullOrWhiteSpace($RelativePath) -or
        $RelativePath.Contains('\') -or
        $RelativePath.StartsWith('/', [StringComparison]::Ordinal) -or
        $RelativePath.Contains(':') -or
        $RelativePath.IndexOf([char]0) -ge 0) {
        Stop-Installer 'CHECKSUM_PATH_INVALID'
    }

    $segments = @($RelativePath.Split('/'))
    if ($segments.Count -eq 0) {
        Stop-Installer 'CHECKSUM_PATH_INVALID'
    }
    foreach ($segment in $segments) {
        if ([string]::IsNullOrWhiteSpace($segment) -or $segment -eq '.' -or $segment -eq '..') {
            Stop-Installer 'CHECKSUM_PATH_INVALID'
        }
    }

    try {
        $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd([IO.Path]::DirectorySeparatorChar)
        $joined = Join-Path $rootFull ($RelativePath.Replace('/', [IO.Path]::DirectorySeparatorChar))
        $resolved = [IO.Path]::GetFullPath($joined)
    }
    catch {
        Stop-Installer 'CHECKSUM_PATH_INVALID'
    }

    if (-not $resolved.StartsWith(
            "$rootFull$([IO.Path]::DirectorySeparatorChar)",
            [StringComparison]::OrdinalIgnoreCase)) {
        Stop-Installer 'CHECKSUM_PATH_INVALID'
    }
    return $resolved
}

function Get-RelativeFileInventory {
    param(
        [Parameter(Mandatory)][string]$Root,
        [switch]$Installed
    )

    $seen = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    $relativePaths = [Collections.Generic.List[string]]::new()
    $files = @(Get-SafeTreeFiles $Root 'BUNDLE_UNREADABLE')

    foreach ($file in $files) {
        if (($file.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            Stop-Installer 'REPARSE_POINT_NOT_ALLOWED'
        }
        $relative = [IO.Path]::GetRelativePath($Root, $file.FullName).Replace('\', '/')
        $null = Get-SafeChildPath $Root $relative
        if (-not $seen.Add($relative)) {
            Stop-Installer 'FILESYSTEM_PATH_DUPLICATE'
        }
        if ($relative -eq $checksumName) {
            continue
        }
        if ($Installed -and $relative -eq $receiptName) {
            continue
        }
        $relativePaths.Add($relative)
    }

    [string[]]$sorted = $relativePaths.ToArray()
    [Array]::Sort($sorted, [StringComparer]::Ordinal)
    return $sorted
}

function Read-ChecksumManifest {
    param(
        [Parameter(Mandatory)][string]$BundleRoot,
        [switch]$Installed
    )

    $checksumPath = Join-Path $BundleRoot $checksumName
    if (-not [IO.File]::Exists($checksumPath) -or (Test-ReparsePoint $checksumPath)) {
        Stop-Installer 'CHECKSUM_MANIFEST_MISSING'
    }

    try {
        $lines = [IO.File]::ReadAllLines($checksumPath, [Text.UTF8Encoding]::new($false, $true))
    }
    catch {
        Stop-Installer 'CHECKSUM_MANIFEST_INVALID'
    }

    $checksums = [Collections.Generic.Dictionary[string, string]]::new(
        [StringComparer]::OrdinalIgnoreCase)
    $orderedPaths = [Collections.Generic.List[string]]::new()
    $signatureBlock = $false
    foreach ($line in $lines) {
        if ($line -eq '# SIG # Begin signature block') {
            if ($signatureBlock) {
                Stop-Installer 'CHECKSUM_MANIFEST_INVALID'
            }
            $signatureBlock = $true
            continue
        }
        if ($signatureBlock) {
            if (-not $line.StartsWith('# ', [StringComparison]::Ordinal)) {
                Stop-Installer 'CHECKSUM_MANIFEST_INVALID'
            }
            continue
        }
        if ($line -notmatch '^# SHA256 (?<Hash>[0-9a-f]{64}) \*(?<Path>.+)$') {
            Stop-Installer 'CHECKSUM_MANIFEST_INVALID'
        }
        $relative = [string]$Matches.Path
        $null = Get-SafeChildPath $BundleRoot $relative
        if ($relative -eq $checksumName -or $relative -eq $receiptName) {
            Stop-Installer 'CHECKSUM_PATH_INVALID'
        }
        if ($checksums.ContainsKey($relative)) {
            Stop-Installer 'CHECKSUM_DUPLICATE'
        }
        $checksums.Add($relative, ([string]$Matches.Hash).ToLowerInvariant())
        $orderedPaths.Add($relative)
    }

    if ($checksums.Count -eq 0) {
        Stop-Installer 'CHECKSUM_MANIFEST_INVALID'
    }
    [string[]]$declaredOrder = $orderedPaths.ToArray()
    [string[]]$sortedOrder = $orderedPaths.ToArray()
    [Array]::Sort($sortedOrder, [StringComparer]::Ordinal)
    if (($declaredOrder -join "`n") -cne ($sortedOrder -join "`n")) {
        Stop-Installer 'CHECKSUM_ORDER_INVALID'
    }

    [string[]]$inventory = Get-RelativeFileInventory $BundleRoot -Installed:$Installed
    if (($inventory -join "`n") -cne ($sortedOrder -join "`n")) {
        Stop-Installer 'CHECKSUM_COVERAGE_INVALID'
    }

    foreach ($relative in $sortedOrder) {
        $absolute = Get-SafeChildPath $BundleRoot $relative
        if (-not [IO.File]::Exists($absolute) -or (Test-ReparsePoint $absolute)) {
            Stop-Installer 'CHECKSUM_COVERAGE_INVALID'
        }
        try {
            $actual = (Get-FileHash -LiteralPath $absolute -Algorithm SHA256).Hash.ToLowerInvariant()
        }
        catch {
            Stop-Installer 'CHECKSUM_READ_FAILED'
        }
        if (-not [Security.Cryptography.CryptographicOperations]::FixedTimeEquals(
                [Convert]::FromHexString($checksums[$relative]),
                [Convert]::FromHexString($actual))) {
            Stop-Installer 'CHECKSUM_MISMATCH'
        }
    }

    return [pscustomobject]@{
        Path = $checksumPath
        Checksums = $checksums
        Files = $sortedOrder
        HasSignatureBlock = $signatureBlock
        Sha256 = (Get-FileHash -LiteralPath $checksumPath -Algorithm SHA256).Hash.ToLowerInvariant()
    }
}

function Assert-ExactXmlAttributes {
    param(
        [Parameter(Mandatory)][Xml.XmlElement]$Element,
        [Parameter(Mandatory)][AllowEmptyCollection()][string[]]$Expected
    )

    [string[]]$actual = @($Element.Attributes | ForEach-Object { $_.Name })
    [string[]]$expectedSorted = @($Expected)
    [Array]::Sort($actual, [StringComparer]::Ordinal)
    [Array]::Sort($expectedSorted, [StringComparer]::Ordinal)
    if (($actual -join "`n") -cne ($expectedSorted -join "`n")) {
        Stop-Installer 'PACKAGE_MANIFEST_INVALID'
    }
}

function Assert-ExactXmlChildren {
    param(
        [Parameter(Mandatory)][Xml.XmlElement]$Element,
        [Parameter(Mandatory)][AllowEmptyCollection()][string[]]$Expected
    )

    $names = [Collections.Generic.List[string]]::new()
    foreach ($node in $Element.ChildNodes) {
        if ($node.NodeType -eq [Xml.XmlNodeType]::Element) {
            $names.Add($node.LocalName)
        }
        elseif ($node.NodeType -ne [Xml.XmlNodeType]::Whitespace -and
            $node.NodeType -ne [Xml.XmlNodeType]::SignificantWhitespace) {
            Stop-Installer 'PACKAGE_MANIFEST_INVALID'
        }
    }
    if (($names.ToArray() -join "`n") -cne ($Expected -join "`n")) {
        Stop-Installer 'PACKAGE_MANIFEST_INVALID'
    }
}

function Read-SafePackageManifest {
    param(
        [Parameter(Mandatory)][string]$BundleRoot,
        [Parameter(Mandatory)][string]$ExpectedSeries
    )

    $manifestPath = Join-Path $BundleRoot 'PackageContents.xml'
    if (-not [IO.File]::Exists($manifestPath) -or (Test-ReparsePoint $manifestPath)) {
        Stop-Installer 'PACKAGE_MANIFEST_MISSING'
    }

    $settings = [Xml.XmlReaderSettings]::new()
    $settings.DtdProcessing = [Xml.DtdProcessing]::Prohibit
    $settings.XmlResolver = $null
    try {
        $reader = [Xml.XmlReader]::Create($manifestPath, $settings)
        $document = [Xml.XmlDocument]::new()
        $document.XmlResolver = $null
        try {
            $document.Load($reader)
        }
        finally {
            $reader.Dispose()
        }
    }
    catch {
        Stop-Installer 'PACKAGE_MANIFEST_INVALID'
    }

    $root = $document.DocumentElement
    if ($null -eq $root -or $root.LocalName -ne 'ApplicationPackage' -or
        $root.GetAttribute('SchemaVersion') -ne '1.0' -or
        $root.GetAttribute('Name') -ne 'AutoCAD Mechanical Harness Bridge' -or
        $root.GetAttribute('Author') -ne 'AutoCAD Mechanical Harness Team' -or
        $root.GetAttribute('Description') -ne
            'Local per-user Named Pipe bridge for atomic mechanical drawing jobs.') {
        Stop-Installer 'PACKAGE_MANIFEST_INVALID'
    }
    Assert-ExactXmlAttributes $root @(
        'SchemaVersion', 'AppVersion', 'Author', 'Name', 'Description', 'ProductCode',
        'UpgradeCode'
    )
    Assert-ExactXmlChildren $root @('CompanyDetails', 'Components')
    $appVersionText = $root.GetAttribute('AppVersion')
    if ($appVersionText -notmatch '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$') {
        Stop-Installer 'PACKAGE_VERSION_INVALID'
    }
    try {
        $appVersion = [Version]::new($appVersionText)
    }
    catch {
        Stop-Installer 'PACKAGE_VERSION_INVALID'
    }

    $productCodeText = $root.GetAttribute('ProductCode')
    $upgradeCodeText = $root.GetAttribute('UpgradeCode')
    $productCode = [Guid]::Empty
    $upgradeCode = [Guid]::Empty
    if (-not [Guid]::TryParse($productCodeText, [ref]$productCode) -or
        -not [Guid]::TryParse($upgradeCodeText, [ref]$upgradeCode) -or
        $productCode -eq [Guid]::Empty -or $productCode -eq $upgradeCode -or
        $productCodeText -cne "{$($productCode.ToString().ToUpperInvariant())}" -or
        $upgradeCodeText -cne "{$($upgradeCode.ToString().ToUpperInvariant())}" -or
        $upgradeCodeText -cne $stableUpgradeCode) {
        Stop-Installer 'PACKAGE_IDENTITY_INVALID'
    }

    $companyDetails = @($root.SelectNodes('./CompanyDetails'))
    $components = @($root.SelectNodes('./Components'))
    $runtime = @($root.SelectNodes('./Components/RuntimeRequirements'))
    $entries = @($root.SelectNodes('./Components/ComponentEntry'))
    if ($companyDetails.Count -ne 1 -or $components.Count -ne 1 -or
        $runtime.Count -ne 1 -or $entries.Count -ne 1) {
        Stop-Installer 'PACKAGE_COMPONENT_INVALID'
    }
    Assert-ExactXmlAttributes $companyDetails[0] @('Name', 'Email')
    Assert-ExactXmlChildren $companyDetails[0] @()
    if ($companyDetails[0].GetAttribute('Name') -ne 'AutoCAD Mechanical Harness Team' -or
        [string]::IsNullOrWhiteSpace($companyDetails[0].GetAttribute('Email'))) {
        Stop-Installer 'PACKAGE_MANIFEST_INVALID'
    }
    Assert-ExactXmlAttributes $components[0] @()
    Assert-ExactXmlChildren $components[0] @('RuntimeRequirements', 'ComponentEntry')
    Assert-ExactXmlAttributes $runtime[0] @(
        'OS', 'Platform', 'SeriesMin', 'SeriesMax'
    )
    Assert-ExactXmlChildren $runtime[0] @()
    if ($runtime[0].GetAttribute('OS') -ne 'Win64' -or
        $runtime[0].GetAttribute('Platform') -ne 'AutoCAD*' -or
        $runtime[0].GetAttribute('SeriesMin') -ne $ExpectedSeries -or
        $runtime[0].GetAttribute('SeriesMax') -ne $ExpectedSeries) {
        Stop-Installer 'PACKAGE_SERIES_MISMATCH'
    }

    $entry = $entries[0]
    Assert-ExactXmlAttributes $entry @(
        'AppName', 'AppDescription', 'AppType', 'ModuleName', 'PerDocument',
        'LoadReasons'
    )
    Assert-ExactXmlChildren $entry @('Commands')
    if ($entry.GetAttribute('AppName') -ne 'AutoCADHarnessBridge' -or
        $entry.GetAttribute('AppDescription') -ne
            'Atomic local bridge for the AutoCAD Mechanical Harness' -or
        $entry.GetAttribute('AppType') -ne '.Net' -or
        $entry.GetAttribute('ModuleName') -ne './Contents/Windows/AutoCADHarness.dll' -or
        $entry.GetAttribute('PerDocument') -ne 'False' -or
        $entry.GetAttribute('LoadReasons') -ne 'LoadOnAutoCADStartup') {
        Stop-Installer 'PACKAGE_COMPONENT_INVALID'
    }
    $commands = @($entry.SelectNodes('./Commands'))
    $commandEntries = @($entry.SelectNodes('./Commands/Command'))
    if ($commands.Count -eq 1) {
        Assert-ExactXmlAttributes $commands[0] @('GroupName')
        Assert-ExactXmlChildren $commands[0] @('Command')
    }
    if ($commandEntries.Count -eq 1) {
        Assert-ExactXmlAttributes $commandEntries[0] @('Global', 'Local')
        Assert-ExactXmlChildren $commandEntries[0] @()
    }
    if ($commands.Count -ne 1 -or $commands[0].GetAttribute('GroupName') -ne 'CADHARNESS' -or
        $commandEntries.Count -ne 1 -or
        $commandEntries[0].GetAttribute('Global') -ne 'CADHARNESSSTATUS' -or
        $commandEntries[0].GetAttribute('Local') -ne 'CADHARNESSSTATUS') {
        Stop-Installer 'PACKAGE_COMPONENT_INVALID'
    }

    return [pscustomobject]@{
        AppVersion = $appVersion
        AppVersionText = $appVersionText
        ProductCode = "{$($productCode.ToString().ToUpperInvariant())}"
        UpgradeCode = "{$($upgradeCode.ToString().ToUpperInvariant())}"
        AutoCADSeries = $ExpectedSeries
    }
}

function Assert-RequiredAssemblies {
    param([Parameter(Mandatory)][string]$BundleRoot)

    $windowsRoot = Join-Path $BundleRoot 'Contents\Windows'
    if (-not [IO.Directory]::Exists($windowsRoot) -or (Test-ReparsePoint $windowsRoot)) {
        Stop-Installer 'REQUIRED_ASSEMBLY_MISSING'
    }
    foreach ($assembly in $requiredAssemblies) {
        $path = Join-Path $windowsRoot $assembly
        if (-not [IO.File]::Exists($path) -or (Test-ReparsePoint $path)) {
            Stop-Installer 'REQUIRED_ASSEMBLY_MISSING'
        }
    }

    $bundleFiles = @(Get-SafeTreeFiles $BundleRoot 'BUNDLE_UNREADABLE')
    foreach ($dll in @($bundleFiles | Where-Object { $_.Extension -ieq '.dll' })) {
        if ($dll.Name -in $forbiddenRuntimeNames -or
            $dll.Name.StartsWith('Autodesk.', [StringComparison]::OrdinalIgnoreCase)) {
            Stop-Installer 'AUTODESK_RUNTIME_REDISTRIBUTION_FORBIDDEN'
        }
    }
}

function Get-AuthenticodeCmsBytes {
    param([Parameter(Mandatory)][string]$LiteralPath)

    try {
        if ([IO.Path]::GetExtension($LiteralPath) -ieq '.ps1') {
            $lines = [IO.File]::ReadAllLines(
                $LiteralPath, [Text.UTF8Encoding]::new($false, $true))
            $inside = $false
            $encoded = [Text.StringBuilder]::new()
            foreach ($line in $lines) {
                if ($line -eq '# SIG # Begin signature block') {
                    $inside = $true
                    continue
                }
                if ($line -eq '# SIG # End signature block') {
                    break
                }
                if ($inside) {
                    if (-not $line.StartsWith('# ', [StringComparison]::Ordinal)) {
                        Stop-Installer 'SIGNATURE_INVALID'
                    }
                    $null = $encoded.Append($line.Substring(2))
                }
            }
            if (-not $inside -or $encoded.Length -eq 0) {
                Stop-Installer 'SIGNATURE_INVALID'
            }
            return [Convert]::FromBase64String($encoded.ToString())
        }

        [byte[]]$bytes = [IO.File]::ReadAllBytes($LiteralPath)
        if ($bytes.Length -lt 256 -or $bytes[0] -ne 0x4d -or $bytes[1] -ne 0x5a) {
            Stop-Installer 'SIGNATURE_INVALID'
        }
        $peOffset = [BitConverter]::ToInt32($bytes, 0x3c)
        if ($peOffset -lt 0 -or $peOffset + 256 -gt $bytes.Length -or
            [BitConverter]::ToUInt32($bytes, $peOffset) -ne 0x00004550) {
            Stop-Installer 'SIGNATURE_INVALID'
        }
        $optionalOffset = $peOffset + 24
        $magic = [BitConverter]::ToUInt16($bytes, $optionalOffset)
        $securityOffset = if ($magic -eq 0x20b) {
            $optionalOffset + 144
        }
        elseif ($magic -eq 0x10b) {
            $optionalOffset + 128
        }
        else {
            Stop-Installer 'SIGNATURE_INVALID'
        }
        $certificateOffset = [BitConverter]::ToUInt32($bytes, $securityOffset)
        $certificateSize = [BitConverter]::ToUInt32($bytes, $securityOffset + 4)
        if ($certificateOffset -eq 0 -or $certificateSize -lt 8 -or
            [uint64]$certificateOffset + [uint64]$certificateSize -gt $bytes.Length) {
            Stop-Installer 'SIGNATURE_INVALID'
        }
        $recordLength = [BitConverter]::ToUInt32($bytes, [int]$certificateOffset)
        $recordType = [BitConverter]::ToUInt16($bytes, [int]$certificateOffset + 6)
        if ($recordLength -lt 9 -or $recordLength -gt $certificateSize -or $recordType -ne 2) {
            Stop-Installer 'SIGNATURE_INVALID'
        }
        [byte[]]$cmsBytes = [byte[]]::new($recordLength - 8)
        [Array]::Copy($bytes, $certificateOffset + 8, $cmsBytes, 0, $cmsBytes.Length)
        return $cmsBytes
    }
    catch [InvalidOperationException] {
        throw
    }
    catch {
        Stop-Installer 'SIGNATURE_INVALID'
    }
}

function Get-AuthenticodeTimestampUtc {
    param([Parameter(Mandatory)][string]$LiteralPath)

    try {
        $cms = [Security.Cryptography.Pkcs.SignedCms]::new()
        $cms.Decode((Get-AuthenticodeCmsBytes $LiteralPath))
        if ($cms.SignerInfos.Count -ne 1) {
            Stop-Installer 'SIGNATURE_INVALID'
        }
        $signer = $cms.SignerInfos[0]
        foreach ($counterSigner in $signer.CounterSignerInfos) {
            foreach ($attribute in $counterSigner.SignedAttributes) {
                if ($attribute.Oid.Value -eq '1.2.840.113549.1.9.5' -and
                    $attribute.Values.Count -eq 1) {
                    return ([Security.Cryptography.Pkcs.Pkcs9SigningTime]::new(
                            $attribute.Values[0].RawData)).SigningTime.ToUniversalTime()
                }
            }
        }
        foreach ($attribute in $signer.UnsignedAttributes) {
            if ($attribute.Oid.Value -ne '1.3.6.1.4.1.311.3.3.1' -or
                $attribute.Values.Count -ne 1) {
                continue
            }
            $timestampCms = [Security.Cryptography.Pkcs.SignedCms]::new()
            $timestampCms.Decode($attribute.Values[0].RawData)
            $reader = [Formats.Asn1.AsnReader]::new(
                $timestampCms.ContentInfo.Content,
                [Formats.Asn1.AsnEncodingRules]::DER)
            $sequence = $reader.ReadSequence()
            $null = $sequence.ReadInteger()
            $null = $sequence.ReadObjectIdentifier()
            $messageImprint = $sequence.ReadSequence()
            $algorithm = $messageImprint.ReadSequence()
            $null = $algorithm.ReadObjectIdentifier()
            if ($algorithm.HasData) { $null = $algorithm.ReadEncodedValue() }
            $null = $messageImprint.ReadOctetString()
            $null = $sequence.ReadInteger()
            return $sequence.ReadGeneralizedTime().UtcDateTime
        }
    }
    catch [InvalidOperationException] {
        throw
    }
    catch {
        Stop-Installer 'SIGNATURE_INVALID'
    }
    Stop-Installer 'SIGNATURE_TIMESTAMP_REQUIRED'
}

function ConvertTo-StrictDateTimeOffset {
    param([Parameter(Mandatory)]$Value)

    try {
        if ($Value -is [DateTimeOffset]) { return [DateTimeOffset]$Value }
        if ($Value -is [DateTime]) { return [DateTimeOffset]([DateTime]$Value) }
        return [DateTimeOffset]::ParseExact(
            ([string]$Value), 'O', [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::RoundtripKind)
    }
    catch { Stop-Installer 'SIGNATURE_POLICY_FACTS_INVALID' }
}

function Assert-SignaturePolicyFacts {
    param(
        [Parameter(Mandatory)]$Facts,
        [Parameter(Mandatory)][AllowEmptyCollection()][object[]]$ApprovedSigners
    )

    Assert-ExactProperties $Facts @(
        'Status', 'Publisher', 'PublicKeySha256', 'Thumbprint', 'CodeSigningEku',
        'SignerNotBeforeUtc', 'SignerNotAfterUtc', 'TimestampUtc',
        'TimestampTrusted', 'CurrentChainTrusted') 'SIGNATURE_POLICY_FACTS_INVALID'
    if ($Facts.Status -ne 'Valid' -or -not [bool]$Facts.CodeSigningEku -or
        -not [bool]$Facts.TimestampTrusted -or -not [bool]$Facts.CurrentChainTrusted -or
        [string]::IsNullOrWhiteSpace([string]$Facts.TimestampUtc)) {
        Stop-Installer 'SIGNATURE_INVALID'
    }
    $notBefore = ConvertTo-StrictDateTimeOffset $Facts.SignerNotBeforeUtc
    $notAfter = ConvertTo-StrictDateTimeOffset $Facts.SignerNotAfterUtc
    $timestamp = ConvertTo-StrictDateTimeOffset $Facts.TimestampUtc
    if ($timestamp -lt $notBefore -or $timestamp -gt $notAfter) {
        Stop-Installer 'SIGNATURE_TIMESTAMP_OUTSIDE_VALIDITY'
    }
    if ([string]$Facts.PublicKeySha256 -notmatch '^[0-9a-f]{64}$' -or
        [string]$Facts.Thumbprint -notmatch '^[0-9A-F]{40,128}$') {
        Stop-Installer 'SIGNATURE_POLICY_FACTS_INVALID'
    }

    $match = $null
    foreach ($candidate in $ApprovedSigners) {
        Assert-ExactProperties $candidate @(
            'Id', 'Publisher', 'PublicKeySha256', 'AllowedPreviousSignerIds') `
            'SIGNER_POLICY_INVALID'
        if ([string]$candidate.Id -notmatch '^[a-z0-9][a-z0-9._-]{2,63}$' -or
            [string]$candidate.PublicKeySha256 -notmatch '^[0-9a-f]{64}$') {
            Stop-Installer 'SIGNER_POLICY_INVALID'
        }
        if ([string]$candidate.Publisher -ceq [string]$Facts.Publisher -and
            [string]$candidate.PublicKeySha256 -ceq [string]$Facts.PublicKeySha256) {
            if ($null -ne $match) { Stop-Installer 'SIGNER_POLICY_INVALID' }
            $match = $candidate
        }
    }
    if ($null -eq $match) {
        Stop-Installer 'SIGNER_NOT_APPROVED'
    }
    return [pscustomobject]@{
        Id = [string]$match.Id
        Thumbprint = [string]$Facts.Thumbprint
        AllowedPreviousSignerIds = @($match.AllowedPreviousSignerIds)
    }
}

function Get-ProductionSigner {
    param([Parameter(Mandatory)][string]$LiteralPath)

    try {
        $signature = Get-AuthenticodeSignature -LiteralPath $LiteralPath
    }
    catch {
        Stop-Installer 'SIGNATURE_INVALID'
    }
    if ($signature.Status -ne [Management.Automation.SignatureStatus]::Valid -or
        $null -eq $signature.SignerCertificate -or
        $null -eq $signature.TimeStamperCertificate) {
        Stop-Installer 'SIGNATURE_INVALID'
    }
    $timestampUtc = Get-AuthenticodeTimestampUtc $LiteralPath
    $certificate = $signature.SignerCertificate
    $timestampCertificate = $signature.TimeStamperCertificate
    $timestampChain = [Security.Cryptography.X509Certificates.X509Chain]::new()
    $signerChain = [Security.Cryptography.X509Certificates.X509Chain]::new()
    try {
        foreach ($chain in @($timestampChain, $signerChain)) {
            $chain.ChainPolicy.RevocationMode =
                [Security.Cryptography.X509Certificates.X509RevocationMode]::Online
            $chain.ChainPolicy.RevocationFlag =
                [Security.Cryptography.X509Certificates.X509RevocationFlag]::EntireChain
            $chain.ChainPolicy.VerificationFlags =
                [Security.Cryptography.X509Certificates.X509VerificationFlags]::NoFlag
        }
        $timestampChain.ChainPolicy.VerificationTime = [DateTime]::UtcNow
        $signerChain.ChainPolicy.VerificationTime = $timestampUtc
        $timestampTrusted = $timestampChain.Build($timestampCertificate)
        $signerTrusted = $signerChain.Build($certificate)
    }
    finally {
        $timestampChain.Dispose()
        $signerChain.Dispose()
    }
    $facts = [pscustomobject]@{
        Status = 'Valid'
        Publisher = $certificate.Subject
        PublicKeySha256 = [Convert]::ToHexString(
            [Security.Cryptography.SHA256]::HashData($certificate.GetPublicKey())).ToLowerInvariant()
        Thumbprint = $certificate.Thumbprint.ToUpperInvariant()
        CodeSigningEku = [bool]($certificate.EnhancedKeyUsageList.ObjectId.Value -contains
            '1.3.6.1.5.5.7.3.3')
        SignerNotBeforeUtc = ([DateTimeOffset]$certificate.NotBefore.ToUniversalTime()).ToString('O')
        SignerNotAfterUtc = ([DateTimeOffset]$certificate.NotAfter.ToUniversalTime()).ToString('O')
        TimestampUtc = ([DateTimeOffset]$timestampUtc).ToString('O')
        TimestampTrusted = [bool]$timestampTrusted
        CurrentChainTrusted = [bool]$signerTrusted
    }
    return Assert-SignaturePolicyFacts $facts @($approvedReleaseSigners)
}

function Assert-ProductionSignatures {
    param(
        [Parameter(Mandatory)][string]$BundleRoot,
        [Parameter(Mandatory)][string]$ChecksumPath
    )

    $signedFiles = [Collections.Generic.List[string]]::new()
    $bundleFiles = @(Get-SafeTreeFiles $BundleRoot 'BUNDLE_UNREADABLE')
    foreach ($dll in @($bundleFiles | Where-Object { $_.Extension -ieq '.dll' })) {
        $signedFiles.Add($dll.FullName)
    }
    $signedFiles.Add($ChecksumPath)
    $signer = $null
    foreach ($path in $signedFiles) {
        $candidate = Get-ProductionSigner $path
        if ($null -eq $signer) {
            $signer = $candidate
        }
        elseif ($candidate.Id -cne $signer.Id -or
            $candidate.Thumbprint -cne $signer.Thumbprint) {
            Stop-Installer 'SIGNER_MISMATCH'
        }
    }
    return $signer
}

function Assert-InstallerReleaseIdentity {
    $scriptSigner = Get-ProductionSigner $PSCommandPath
    return $scriptSigner
}

function Assert-ApprovedSignerRotation {
    param(
        [Parameter(Mandatory)][string]$PreviousSignerId,
        [Parameter(Mandatory)]$NewSigner
    )

    if ($PreviousSignerId -ceq [string]$NewSigner.Id) { return }
    if (-not (@($NewSigner.AllowedPreviousSignerIds) -ccontains $PreviousSignerId)) {
        Stop-Installer 'UPGRADE_SIGNER_ROTATION_NOT_APPROVED'
    }
}

function Assert-DevelopmentSignaturePolicyFixture {
    param([Parameter(Mandatory)][string]$FixturePath)

    $resolved = Get-FullLocalPath $FixturePath 'SIGNATURE_POLICY_FIXTURE_INVALID'
    if (-not [IO.File]::Exists($resolved) -or (Test-ReparsePoint $resolved)) {
        Stop-Installer 'SIGNATURE_POLICY_FIXTURE_INVALID'
    }
    Assert-NoAlternateDataStreams $resolved
    try {
        $fixture = [IO.File]::ReadAllText(
            $resolved, [Text.UTF8Encoding]::new($false, $true)) | ConvertFrom-Json
    }
    catch { Stop-Installer 'SIGNATURE_POLICY_FIXTURE_INVALID' }
    Assert-ExactProperties $fixture @(
        'Facts', 'ApprovedSigners', 'InstallerSignerId', 'PreviousSignerId') `
        'SIGNATURE_POLICY_FIXTURE_INVALID'
    $signer = Assert-SignaturePolicyFacts $fixture.Facts @($fixture.ApprovedSigners)
    if ([string]$fixture.InstallerSignerId -cne [string]$signer.Id) {
        Stop-Installer 'INSTALLER_SIGNER_MISMATCH'
    }
    if (-not [string]::IsNullOrWhiteSpace([string]$fixture.PreviousSignerId)) {
        Assert-ApprovedSignerRotation ([string]$fixture.PreviousSignerId) $signer
    }
    return $signer
}

function Assert-ExactProperties {
    param(
        [Parameter(Mandatory)]$Value,
        [Parameter(Mandatory)][string[]]$Expected,
        [Parameter(Mandatory)][string]$ErrorCode
    )

    [string[]]$actual = @($Value.PSObject.Properties.Name)
    [string[]]$expectedSorted = @($Expected)
    [Array]::Sort($actual, [StringComparer]::Ordinal)
    [Array]::Sort($expectedSorted, [StringComparer]::Ordinal)
    if (($actual -join "`n") -cne ($expectedSorted -join "`n")) {
        Stop-Installer $ErrorCode
    }
}

function Read-InstallReceipt {
    param(
        [Parameter(Mandatory)][string]$BundleRoot,
        [Parameter(Mandatory)]$Manifest,
        [Parameter(Mandatory)]$Checksum,
        [Parameter(Mandatory)][string]$ArtifactKind
    )

    $receiptPath = Join-Path $BundleRoot $receiptName
    if (-not [IO.File]::Exists($receiptPath) -or (Test-ReparsePoint $receiptPath)) {
        Stop-Installer 'INSTALL_RECEIPT_MISSING'
    }
    try {
        $receipt = [IO.File]::ReadAllText(
            $receiptPath,
            [Text.UTF8Encoding]::new($false, $true)) | ConvertFrom-Json
    }
    catch {
        Stop-Installer 'INSTALL_RECEIPT_INVALID'
    }

    Assert-ExactProperties $receipt @(
        'SchemaVersion', 'Owner', 'BundleName', 'ArtifactKind', 'AutoCADSeries',
        'AppVersion', 'ProductCode', 'UpgradeCode', 'ChecksumManifestSha256',
        'SignerId', 'Files', 'Directories') `
        'INSTALL_RECEIPT_INVALID'
    if ($receipt.SchemaVersion -ne '2.0' -or
        $receipt.Owner -ne 'autocad-mechanical-harness' -or
        $receipt.BundleName -ne $bundleName -or
        $receipt.ArtifactKind -ne $ArtifactKind -or
        $receipt.AutoCADSeries -ne $Manifest.AutoCADSeries -or
        $receipt.AppVersion -ne $Manifest.AppVersionText -or
        $receipt.ProductCode -ne $Manifest.ProductCode -or
        $receipt.UpgradeCode -ne $Manifest.UpgradeCode -or
        $receipt.ChecksumManifestSha256 -ne $Checksum.Sha256 -or
        [string]$receipt.SignerId -ne [string]$script:currentValidationSignerId) {
        Stop-Installer 'INSTALL_RECEIPT_INVALID'
    }

    $receiptFiles = @($receipt.Files)
    if ($receiptFiles.Count -ne $Checksum.Files.Count) {
        Stop-Installer 'INSTALL_RECEIPT_INVALID'
    }
    for ($index = 0; $index -lt $receiptFiles.Count; $index++) {
        $file = $receiptFiles[$index]
        Assert-ExactProperties $file @('RelativePath', 'Sha256') 'INSTALL_RECEIPT_INVALID'
        $relative = [string]$Checksum.Files[$index]
        if ($file.RelativePath -cne $relative -or $file.Sha256 -cne $Checksum.Checksums[$relative]) {
            Stop-Installer 'INSTALL_RECEIPT_INVALID'
        }
    }
    [string[]]$receiptDirectories = @($receipt.Directories | ForEach-Object { [string]$_ })
    [string[]]$actualDirectories = Get-RelativeDirectoryInventory $BundleRoot
    if (($receiptDirectories -join "`n") -cne ($actualDirectories -join "`n")) {
        Stop-Installer 'INSTALL_RECEIPT_INVALID'
    }
    foreach ($relative in $receiptDirectories) {
        $null = Get-SafeChildPath $BundleRoot "$relative/owned-placeholder"
    }
    return $receipt
}

function Get-BundleValidation {
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][string]$ExpectedSeries,
        [Parameter(Mandatory)][bool]$UnsignedDevelopment,
        [switch]$Installed,
        [switch]$AllowStagingName
    )

    $bundleRoot = Get-FullLocalPath $Root 'BUNDLE_PATH_INVALID'
    Assert-ExistingDirectory $bundleRoot 'BUNDLE_NOT_FOUND'
    if (-not $AllowStagingName -and
        [IO.Path]::GetFileName($bundleRoot) -cne $bundleName) {
        Stop-Installer 'BUNDLE_NAME_INVALID'
    }
    Assert-NoReparseTree $bundleRoot

    $receiptPath = Join-Path $bundleRoot $receiptName
    if (-not $Installed -and [IO.File]::Exists($receiptPath)) {
        Stop-Installer 'SOURCE_BUNDLE_CONTAINS_RECEIPT'
    }
    $markerPath = Join-Path $bundleRoot $developmentMarkerName
    $hasMarker = [IO.File]::Exists($markerPath)
    if ($hasMarker -and -not $UnsignedDevelopment) {
        Stop-Installer 'DEVELOPMENT_SWITCH_REQUIRED'
    }
    if ($UnsignedDevelopment -and -not $hasMarker) {
        Stop-Installer 'DEVELOPMENT_MARKER_REQUIRED'
    }

    $manifest = Read-SafePackageManifest $bundleRoot $ExpectedSeries
    Assert-RequiredAssemblies $bundleRoot
    $checksum = Read-ChecksumManifest $bundleRoot -Installed:$Installed
    $artifactKind = if ($UnsignedDevelopment) { 'DEVELOPMENT-UNSIGNED' } else { 'RELEASE-SIGNED' }
    $signer = $null
    if ($UnsignedDevelopment) {
        if ($checksum.HasSignatureBlock) {
            Stop-Installer 'DEVELOPMENT_CHECKSUM_MUST_BE_UNSIGNED'
        }
    }
    else {
        if (-not $checksum.HasSignatureBlock) {
            Stop-Installer 'SIGNATURE_INVALID'
        }
        $signer = Assert-ProductionSignatures $bundleRoot $checksum.Path
    }

    $receipt = $null
    $script:currentValidationSignerId = if ($null -eq $signer) { '' } else { [string]$signer.Id }
    if ($Installed) {
        $receipt = Read-InstallReceipt $bundleRoot $manifest $checksum $artifactKind
    }
    [string[]]$directories = Get-RelativeDirectoryInventory $bundleRoot
    return [pscustomobject]@{
        Root = $bundleRoot
        Manifest = $manifest
        Checksum = $checksum
        ArtifactKind = $artifactKind
        SignerThumbprint = if ($null -eq $signer) { $null } else { $signer.Thumbprint }
        SignerId = if ($null -eq $signer) { '' } else { $signer.Id }
        Signer = $signer
        Receipt = $receipt
        Files = @($checksum.Files) + @($checksumName)
        Directories = $directories
        ReceiptSha256 = if ($Installed) {
            (Get-FileHash -LiteralPath (Join-Path $bundleRoot $receiptName) -Algorithm SHA256).Hash.ToLowerInvariant()
        }
        else { '' }
    }
}

function Assert-DevelopmentContext {
    param([Parameter(Mandatory)][string]$ResolvedInstallRoot)

    $insideDefault = Test-PathEqualOrWithin $ResolvedInstallRoot $defaultInstallRoot
    if ($DevelopmentUnsigned -and $insideDefault) {
        Stop-Installer 'DEVELOPMENT_CUSTOM_ROOT_REQUIRED'
    }
    if ($AllowRunningAutoCADForDevelopmentTest -and
        (-not $DevelopmentUnsigned -or $insideDefault)) {
        Stop-Installer 'AUTOCAD_BYPASS_NOT_ALLOWED'
    }
    $hasFaultInjection = $DevelopmentTestFault -ne 'None' -or
        $DevelopmentTestHoldMutexMilliseconds -gt 0 -or
        -not [string]::IsNullOrWhiteSpace($DevelopmentTestPreCommitBarrierPath)
    if ($hasFaultInjection -and
        (-not $DevelopmentUnsigned -or -not $AllowRunningAutoCADForDevelopmentTest -or
            $insideDefault)) {
        Stop-Installer 'DEVELOPMENT_TEST_HOOK_NOT_ALLOWED'
    }
    if (-not [string]::IsNullOrWhiteSpace($DevelopmentTestSignaturePolicyFixture) -and
        (-not $DevelopmentUnsigned -or $insideDefault -or
            ($Action -ne 'Validate' -and -not $AllowRunningAutoCADForDevelopmentTest))) {
        Stop-Installer 'DEVELOPMENT_TEST_HOOK_NOT_ALLOWED'
    }

    $pathRoot = [IO.Path]::GetPathRoot($ResolvedInstallRoot)
    if ($ResolvedInstallRoot.TrimEnd([IO.Path]::DirectorySeparatorChar).Equals(
            $pathRoot.TrimEnd([IO.Path]::DirectorySeparatorChar),
            [StringComparison]::OrdinalIgnoreCase)) {
        Stop-Installer 'INSTALL_ROOT_TOO_BROAD'
    }
}

function Assert-AutoCADStopped {
    param([Parameter(Mandatory)][string]$ResolvedInstallRoot)

    try {
        $running = @(Get-Process -ErrorAction Stop | Where-Object {
                $_.ProcessName.Equals('acad', [StringComparison]::OrdinalIgnoreCase)
            })
    }
    catch {
        Stop-Installer 'AUTOCAD_PROCESS_CHECK_FAILED'
    }
    if ($running.Count -eq 0) {
        return
    }
    $insideDefault = Test-PathEqualOrWithin $ResolvedInstallRoot $defaultInstallRoot
    if (-not ($DevelopmentUnsigned -and -not $insideDefault -and
            $AllowRunningAutoCADForDevelopmentTest)) {
        Stop-Installer 'AUTOCAD_RUNNING'
    }
}

function Get-JournalKey {
    param(
        [Parameter(Mandatory)][string]$InstallRootPath,
        [switch]$RequireExisting
    )

    Assert-InstallRootTransactionLock $InstallRootPath
    $keyPath = Join-Path $InstallRootPath $journalKeyName
    [byte[]]$entropy = [Text.UTF8Encoding]::new($false).GetBytes(
        'cad-harness-installer-journal-v1')
    if ([IO.File]::Exists($keyPath)) {
        if (Test-ReparsePoint $keyPath) { Stop-Installer 'JOURNAL_KEY_INVALID' }
        Assert-NoAlternateDataStreams $keyPath
        try {
            [byte[]]$protected = [IO.File]::ReadAllBytes($keyPath)
            [byte[]]$key = [Security.Cryptography.ProtectedData]::Unprotect(
                $protected, $entropy,
                [Security.Cryptography.DataProtectionScope]::CurrentUser)
        }
        catch {
            Stop-Installer 'JOURNAL_KEY_INVALID'
        }
        if ($key.Length -ne 32) { Stop-Installer 'JOURNAL_KEY_INVALID' }
        return $key
    }
    if ($RequireExisting) {
        Stop-Installer 'JOURNAL_KEY_INVALID'
    }

    [byte[]]$newKey = [byte[]]::new(32)
    [Security.Cryptography.RandomNumberGenerator]::Fill($newKey)
    try {
        [byte[]]$protectedKey = [Security.Cryptography.ProtectedData]::Protect(
            $newKey, $entropy,
            [Security.Cryptography.DataProtectionScope]::CurrentUser)
        $stream = [IO.FileStream]::new(
            $keyPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try {
            $stream.Write($protectedKey, 0, $protectedKey.Length)
            $stream.Flush($true)
        }
        finally { $stream.Dispose() }
        Assert-NoAlternateDataStreams $keyPath
    }
    catch {
        Stop-Installer 'JOURNAL_KEY_WRITE_FAILED'
    }
    return $newKey
}

function Enter-InstallRootMutex {
    param([Parameter(Mandatory)]$RootIdentity)

    $identityText = '{0}|{1}|{2}' -f
        $RootIdentity.CanonicalPath.ToLowerInvariant(),
        $RootIdentity.VolumeSerial,
        $RootIdentity.FileId
    $mutexName = "Local\CadHarnessInstaller-$((Get-Sha256Text $identityText).Substring(0, 40))"
    try {
        $mutex = [Threading.Mutex]::new($false, $mutexName)
        try { $acquired = $mutex.WaitOne(0) }
        catch [Threading.AbandonedMutexException] { $acquired = $true }
    }
    catch {
        Stop-Installer 'INSTALL_MUTEX_FAILED'
    }
    if (-not $acquired) {
        $mutex.Dispose()
        Stop-Installer 'INSTALL_ROOT_BUSY'
    }
    return $mutex
}

function Get-NativeHandleIdentity {
    param(
        [Parameter(Mandatory)]$Handle,
        [Parameter(Mandatory)][string]$ErrorCode
    )

    try {
        [string[]]$identity = [CadHarnessInstallerNative]::GetHandleIdentity($Handle)
        $canonical = Get-FullLocalPath `
            (ConvertFrom-FinalHandlePath $identity[0]) $ErrorCode
        $attributes = [Convert]::ToUInt32($identity[3], 16)
        $links = [Convert]::ToUInt32($identity[4])
    }
    catch [InvalidOperationException] { throw }
    catch { Stop-Installer $ErrorCode }
    return [pscustomobject]@{
        CanonicalPath = $canonical
        VolumeSerial = $identity[1]
        FileId = $identity[2]
        Attributes = $attributes
        NumberOfLinks = $links
    }
}

function Assert-InstallRootTransactionLock {
    param([Parameter(Mandatory)][string]$InstallRootPath)

    if ($null -eq $script:installRootLock -or
        $null -eq $script:installRootIdentity -or
        $script:installRootLock.RootHandle.IsClosed -or
        $script:installRootLock.RootHandle.IsInvalid -or
        $script:installRootLock.LockHandle.IsClosed -or
        $script:installRootLock.LockHandle.IsInvalid) {
        Stop-Installer 'INSTALL_LOCK_NOT_HELD'
    }
    $expectedRoot = Get-FullLocalPath $InstallRootPath 'INSTALL_ROOT_IDENTITY_CHANGED'
    if ($expectedRoot -cne $script:installRootLock.RootPath) {
        Stop-Installer 'INSTALL_ROOT_IDENTITY_CHANGED'
    }
    $rootHandleIdentity = Get-NativeHandleIdentity `
        $script:installRootLock.RootHandle 'INSTALL_ROOT_IDENTITY_CHANGED'
    $lockHandleIdentity = Get-NativeHandleIdentity `
        $script:installRootLock.LockHandle 'INSTALL_LOCK_IDENTITY_CHANGED'
    $currentRootIdentity = Get-ExistingDirectoryIdentity `
        $InstallRootPath 'INSTALL_ROOT_IDENTITY_CHANGED'
    if ($rootHandleIdentity.CanonicalPath -cne $script:installRootLock.RootPath -or
        $rootHandleIdentity.VolumeSerial -cne $script:installRootLock.RootVolumeSerial -or
        $rootHandleIdentity.FileId -cne $script:installRootLock.RootFileId -or
        ($rootHandleIdentity.Attributes -band [uint32][IO.FileAttributes]::Directory) -eq 0 -or
        ($rootHandleIdentity.Attributes -band [uint32][IO.FileAttributes]::ReparsePoint) -ne 0 -or
        $currentRootIdentity.CanonicalPath -cne $script:installRootLock.RootPath -or
        $currentRootIdentity.VolumeSerial -cne $script:installRootLock.RootVolumeSerial -or
        $currentRootIdentity.FileId -cne $script:installRootLock.RootFileId) {
        Stop-Installer 'INSTALL_ROOT_IDENTITY_CHANGED'
    }
    if ($lockHandleIdentity.CanonicalPath -cne $script:installRootLock.LockPath -or
        $lockHandleIdentity.VolumeSerial -cne $script:installRootLock.LockVolumeSerial -or
        $lockHandleIdentity.FileId -cne $script:installRootLock.LockFileId -or
        $lockHandleIdentity.NumberOfLinks -ne 1 -or
        ($lockHandleIdentity.Attributes -band [uint32][IO.FileAttributes]::Directory) -ne 0 -or
        ($lockHandleIdentity.Attributes -band [uint32][IO.FileAttributes]::ReparsePoint) -ne 0) {
        Stop-Installer 'INSTALL_LOCK_IDENTITY_CHANGED'
    }
    try {
        $hasAlternateStreams = [CadHarnessInstallerNative]::HasAlternateDataStreams(
            $script:installRootLock.LockHandle)
    }
    catch { Stop-Installer 'INSTALL_LOCK_INVALID' }
    if ($hasAlternateStreams) {
        Stop-Installer 'ALTERNATE_DATA_STREAM_NOT_ALLOWED'
    }
}

function Enter-InstallRootTransactionLock {
    param(
        [Parameter(Mandatory)][string]$InstallRootPath,
        [Parameter(Mandatory)]$RootIdentity,
        [switch]$RequireExisting
    )

    $lockPath = Join-Path $InstallRootPath $transactionLockName
    $expectedLockPath = Get-FullLocalPath `
        (Join-Path $InstallRootPath $transactionLockName) 'INSTALL_LOCK_INVALID'
    if ((Get-FullLocalPath $lockPath 'INSTALL_LOCK_INVALID') -cne $expectedLockPath -or
        -not ([IO.Path]::GetDirectoryName($expectedLockPath)).Equals(
            (Get-FullLocalPath $InstallRootPath 'INSTALL_ROOT_IDENTITY_CHANGED'),
            [StringComparison]::OrdinalIgnoreCase)) {
        Stop-Installer 'INSTALL_LOCK_INVALID'
    }
    if ([IO.Directory]::Exists($lockPath)) {
        if (Test-ReparsePoint $lockPath) {
            Stop-Installer 'REPARSE_POINT_NOT_ALLOWED'
        }
        Stop-Installer 'INSTALL_LOCK_INVALID'
    }
    $lockExists = [IO.File]::Exists($lockPath)
    if ($lockExists) {
        if (Test-ReparsePoint $lockPath) {
            Stop-Installer 'REPARSE_POINT_NOT_ALLOWED'
        }
        Assert-NoAlternateDataStreams $lockPath
    }
    elseif ($RequireExisting) {
        Stop-Installer 'INSTALL_LOCK_INVALID'
    }

    $rootHandle = $null
    $lockHandle = $null
    try {
        $rootHandle = [CadHarnessInstallerNative]::OpenDirectoryHandle($InstallRootPath)
        $rootHandleIdentity = Get-NativeHandleIdentity `
            $rootHandle 'INSTALL_ROOT_IDENTITY_CHANGED'
        if ($rootHandleIdentity.CanonicalPath -cne $RootIdentity.CanonicalPath -or
            $rootHandleIdentity.VolumeSerial -cne $RootIdentity.VolumeSerial -or
            $rootHandleIdentity.FileId -cne $RootIdentity.FileId -or
            ($rootHandleIdentity.Attributes -band [uint32][IO.FileAttributes]::Directory) -eq 0 -or
            ($rootHandleIdentity.Attributes -band [uint32][IO.FileAttributes]::ReparsePoint) -ne 0) {
            Stop-Installer 'INSTALL_ROOT_IDENTITY_CHANGED'
        }
        $lockHandle = [CadHarnessInstallerNative]::OpenExclusiveTransactionLock(
            $lockPath, [bool]$RequireExisting)
        $lockIdentity = Get-NativeHandleIdentity $lockHandle 'INSTALL_LOCK_INVALID'
        if ($lockIdentity.CanonicalPath -cne (Get-FullLocalPath $lockPath 'INSTALL_LOCK_INVALID') -or
            $lockIdentity.VolumeSerial -cne $RootIdentity.VolumeSerial -or
            $lockIdentity.NumberOfLinks -ne 1 -or
            ($lockIdentity.Attributes -band [uint32][IO.FileAttributes]::Directory) -ne 0 -or
            ($lockIdentity.Attributes -band [uint32][IO.FileAttributes]::ReparsePoint) -ne 0) {
            Stop-Installer 'INSTALL_LOCK_INVALID'
        }
        if ([CadHarnessInstallerNative]::HasAlternateDataStreams($lockHandle)) {
            Stop-Installer 'ALTERNATE_DATA_STREAM_NOT_ALLOWED'
        }
        $script:installRootIdentity = $RootIdentity
        $script:installRootLock = [pscustomobject]@{
            RootPath = $RootIdentity.CanonicalPath
            RootVolumeSerial = $RootIdentity.VolumeSerial
            RootFileId = $RootIdentity.FileId
            RootHandle = $rootHandle
            LockPath = $lockIdentity.CanonicalPath
            LockVolumeSerial = $lockIdentity.VolumeSerial
            LockFileId = $lockIdentity.FileId
            LockHandle = $lockHandle
        }
        $rootHandle = $null
        $lockHandle = $null
        Assert-InstallRootTransactionLock $InstallRootPath
        if ($DevelopmentTestHoldMutexMilliseconds -gt 0) {
            Start-Sleep -Milliseconds $DevelopmentTestHoldMutexMilliseconds
        }
        return $script:installRootLock
    }
    catch [InvalidOperationException] {
        if ($null -ne $lockHandle) { $lockHandle.Dispose() }
        if ($null -ne $rootHandle) { $rootHandle.Dispose() }
        throw
    }
    catch {
        if ($null -ne $lockHandle) { $lockHandle.Dispose() }
        if ($null -ne $rootHandle) { $rootHandle.Dispose() }
        $baseException = $_.Exception.GetBaseException()
        if ($baseException -is [ComponentModel.Win32Exception] -and
            $baseException.NativeErrorCode -in @(32, 33)) {
            Stop-Installer 'INSTALL_ROOT_BUSY'
        }
        Stop-Installer 'INSTALL_LOCK_INVALID'
    }
}

function Get-JournalPropertyNames {
    return @(
        'SchemaVersion', 'TransactionId', 'Action', 'Phase',
        'RootCanonicalPathSha256', 'RootVolumeSerial', 'RootFileId',
        'LockVolumeSerial', 'LockFileId',
        'InstallerSignerId', 'InstallerSignerThumbprint',
        'ArtifactKind', 'AutoCADSeries', 'AppVersion', 'ProductCode', 'FileCount',
        'StageName', 'BackupName', 'FailedName', 'QuarantineName',
        'NewRootVolumeSerial', 'NewRootFileId', 'NewReceiptSha256',
        'ExistingRootVolumeSerial', 'ExistingRootFileId', 'ExistingReceiptSha256',
        'NewOwnedFiles', 'NewOwnedDirectories',
        'ExistingOwnedFiles', 'ExistingOwnedDirectories', 'HmacSha256')
}

function Get-JournalMacPayload {
    param([Parameter(Mandatory)]$Journal)

    $ordered = [ordered]@{}
    foreach ($name in (Get-JournalPropertyNames)) {
        if ($name -ne 'HmacSha256') { $ordered[$name] = $Journal.$name }
    }
    return ($ordered | ConvertTo-Json -Depth 8 -Compress)
}

function Get-JournalMac {
    param(
        [Parameter(Mandatory)]$Journal,
        [Parameter(Mandatory)][byte[]]$Key
    )

    $hmac = [Security.Cryptography.HMACSHA256]::new($Key)
    try {
        return [Convert]::ToHexString($hmac.ComputeHash(
                [Text.UTF8Encoding]::new($false).GetBytes(
                    (Get-JournalMacPayload $Journal)))).ToLowerInvariant()
    }
    finally { $hmac.Dispose() }
}

function Write-TransactionJournal {
    param(
        [Parameter(Mandatory)][string]$InstallRootPath,
        [Parameter(Mandatory)]$Journal,
        [Parameter(Mandatory)][byte[]]$Key
    )

    $Journal.HmacSha256 = Get-JournalMac $Journal $Key
    $payload = ($Journal | ConvertTo-Json -Depth 8 -Compress) + "`n"
    $journalPath = Join-Path $InstallRootPath $journalName
    $temporary = Join-Path $InstallRootPath (
        ".cad-harness-installer-journal.tmp.$([Guid]::NewGuid().ToString('N'))")
    try {
        $bytes = [Text.UTF8Encoding]::new($false).GetBytes($payload)
        $stream = [IO.FileStream]::new(
            $temporary, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try {
            $stream.Write($bytes, 0, $bytes.Length)
            $stream.Flush($true)
        }
        finally { $stream.Dispose() }
        Assert-InstallRootTransactionLock $InstallRootPath
        [IO.File]::Move($temporary, $journalPath, $true)
        Assert-NoAlternateDataStreams $journalPath
    }
    catch [InvalidOperationException] { throw }
    catch {
        if ([IO.File]::Exists($temporary)) {
            try { [IO.File]::Delete($temporary) } catch { }
        }
        Stop-Installer 'JOURNAL_WRITE_FAILED'
    }
}

function Clear-TransactionJournal {
    param([Parameter(Mandatory)][string]$InstallRootPath)

    $journalPath = Join-Path $InstallRootPath $journalName
    if (-not [IO.File]::Exists($journalPath)) { return }
    if (Test-ReparsePoint $journalPath) { Stop-Installer 'JOURNAL_INVALID' }
    Assert-NoAlternateDataStreams $journalPath
    try { [IO.File]::Delete($journalPath) }
    catch { Stop-Installer 'JOURNAL_CLEAR_FAILED' }
}

function Assert-JournalRelativePaths {
    param(
        [Parameter(Mandatory)][string]$InstallRootPath,
        [Parameter(Mandatory)][AllowNull()][AllowEmptyCollection()][object[]]$Values
    )

    $seen = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($value in $Values) {
        $relative = [string]$value
        $null = Get-SafeChildPath (Join-Path $InstallRootPath 'owned-root') $relative
        if (-not $seen.Add($relative)) { Stop-Installer 'JOURNAL_INVALID' }
    }
}

function Read-TransactionJournal {
    param(
        [Parameter(Mandatory)][string]$InstallRootPath,
        [Parameter(Mandatory)]$RootIdentity,
        [Parameter(Mandatory)][byte[]]$Key
    )

    $journalPath = Join-Path $InstallRootPath $journalName
    if (-not [IO.File]::Exists($journalPath)) { return $null }
    if (Test-ReparsePoint $journalPath) { Stop-Installer 'JOURNAL_INVALID' }
    Assert-NoAlternateDataStreams $journalPath
    try {
        $journal = [IO.File]::ReadAllText(
            $journalPath, [Text.UTF8Encoding]::new($false, $true)) | ConvertFrom-Json
    }
    catch { Stop-Installer 'JOURNAL_INVALID' }
    Assert-ExactProperties $journal (Get-JournalPropertyNames) 'JOURNAL_INVALID'
    if ($journal.SchemaVersion -ne $journalSchemaVersion -or
        [string]$journal.TransactionId -notmatch '^[0-9a-f]{32}$' -or
        [string]$journal.Action -notin @('install', 'upgrade', 'uninstall') -or
        [string]$journal.Phase -notin @(
            'prepared', 'old_quarantined', 'verified_precommit', 'published',
            'quarantined', 'verified', 'cleanup_pending') -or
        [string]$journal.RootCanonicalPathSha256 -cne
            (Get-Sha256Text $RootIdentity.CanonicalPath.ToLowerInvariant()) -or
        [string]$journal.RootVolumeSerial -cne [string]$RootIdentity.VolumeSerial -or
        [string]$journal.RootFileId -cne [string]$RootIdentity.FileId -or
        [string]$journal.InstallerSignerId -notmatch '^[a-z0-9][a-z0-9._-]{2,63}$' -or
        ([string]$journal.InstallerSignerThumbprint -ne '' -and
            [string]$journal.InstallerSignerThumbprint -notmatch '^[0-9A-F]{40,128}$') -or
        [string]$journal.HmacSha256 -notmatch '^[0-9a-f]{64}$') {
        Stop-Installer 'JOURNAL_INVALID'
    }
    $allowedPhases = switch ([string]$journal.Action) {
        'install' { @('prepared', 'published', 'verified', 'cleanup_pending') }
        'upgrade' {
            @('prepared', 'old_quarantined', 'published', 'verified', 'cleanup_pending')
        }
        'uninstall' {
            @('prepared', 'verified_precommit', 'quarantined', 'cleanup_pending')
        }
    }
    if ([string]$journal.Phase -notin $allowedPhases -or
        [string]$journal.ArtifactKind -notin @('DEVELOPMENT-UNSIGNED', 'RELEASE-SIGNED') -or
        [string]$journal.AutoCADSeries -notin @('R25.0', 'R26.0') -or
        [string]$journal.AppVersion -notmatch '^\d+\.\d+\.\d+\.\d+$' -or
        [string]$journal.ProductCode -notmatch '^\{[0-9A-F-]{36}\}$' -or
        [int]$journal.FileCount -lt 1) {
        Stop-Installer 'JOURNAL_INVALID'
    }
    foreach ($name in @('StageName', 'BackupName', 'FailedName', 'QuarantineName')) {
        $leaf = [string]$journal.$name
        if ($leaf -ne '' -and
            $leaf -notmatch '^\.AutoCADHarness\.bundle\.(stage|backup|failed|uninstall)\.[0-9a-f]{32}$') {
            Stop-Installer 'JOURNAL_INVALID'
        }
    }
    foreach ($name in @(
            'NewOwnedFiles', 'NewOwnedDirectories',
            'ExistingOwnedFiles', 'ExistingOwnedDirectories')) {
        if ($null -ne $journal.$name) {
            Assert-JournalRelativePaths $InstallRootPath @($journal.$name)
        }
    }
    $expectedMac = Get-JournalMac $journal $Key
    if (-not [Security.Cryptography.CryptographicOperations]::FixedTimeEquals(
            [Convert]::FromHexString($expectedMac),
            [Convert]::FromHexString([string]$journal.HmacSha256))) {
        Stop-Installer 'JOURNAL_AUTHENTICATION_FAILED'
    }
    if ([string]$journal.LockVolumeSerial -cne
            [string]$script:installRootLock.LockVolumeSerial -or
        [string]$journal.LockFileId -cne [string]$script:installRootLock.LockFileId) {
        Stop-Installer 'INSTALL_LOCK_IDENTITY_CHANGED'
    }
    return $journal
}

function Set-TransactionPhase {
    param(
        [Parameter(Mandatory)][string]$InstallRootPath,
        [Parameter(Mandatory)]$Journal,
        [Parameter(Mandatory)][string]$Phase,
        [Parameter(Mandatory)][byte[]]$Key
    )

    $Journal.Phase = $Phase
    Write-TransactionJournal $InstallRootPath $Journal $Key
}

function Get-ValidationIdentity {
    param([Parameter(Mandatory)]$Validation)

    $identity = Get-ExistingDirectoryIdentity $Validation.Root 'BUNDLE_PATH_INVALID'
    return [pscustomobject]@{
        RootVolumeSerial = $identity.VolumeSerial
        RootFileId = $identity.FileId
        ReceiptSha256 = [string]$Validation.ReceiptSha256
        OwnedFiles = @($Validation.Files) + @($receiptName)
        OwnedDirectories = @($Validation.Directories)
    }
}

function Assert-ValidationIdentityUnchanged {
    param(
        [Parameter(Mandatory)]$Expected,
        [Parameter(Mandatory)]$Actual
    )

    $identity = Get-ValidationIdentity $Actual
    if ($identity.RootVolumeSerial -cne [string]$Expected.RootVolumeSerial -or
        $identity.RootFileId -cne [string]$Expected.RootFileId -or
        $identity.ReceiptSha256 -cne [string]$Expected.ReceiptSha256 -or
        (@($identity.OwnedFiles) -join "`n") -cne (@($Expected.OwnedFiles) -join "`n") -or
        (@($identity.OwnedDirectories) -join "`n") -cne
            (@($Expected.OwnedDirectories) -join "`n")) {
        Stop-Installer 'TRANSACTION_IDENTITY_CHANGED'
    }
}

function New-TransactionJournal {
    param(
        [Parameter(Mandatory)][string]$TransactionAction,
        [Parameter(Mandatory)]$RootIdentity,
        [Parameter(Mandatory)]$InstallerIdentity,
        $NewValidation,
        $ExistingValidation,
        [Parameter(Mandatory)][AllowEmptyString()][string]$StageName,
        [Parameter(Mandatory)][AllowEmptyString()][string]$BackupName,
        [Parameter(Mandatory)][AllowEmptyString()][string]$FailedName,
        [Parameter(Mandatory)][AllowEmptyString()][string]$QuarantineName
    )

    $newIdentity = if ($null -eq $NewValidation) { $null } else {
        Get-ValidationIdentity $NewValidation
    }
    $existingIdentity = if ($null -eq $ExistingValidation) { $null } else {
        Get-ValidationIdentity $ExistingValidation
    }
    $metadata = if ($TransactionAction -eq 'uninstall') {
        $ExistingValidation
    }
    else { $NewValidation }
    return [pscustomobject][ordered]@{
        SchemaVersion = $journalSchemaVersion
        TransactionId = [Guid]::NewGuid().ToString('N')
        Action = $TransactionAction
        Phase = 'prepared'
        RootCanonicalPathSha256 = Get-Sha256Text $RootIdentity.CanonicalPath.ToLowerInvariant()
        RootVolumeSerial = $RootIdentity.VolumeSerial
        RootFileId = $RootIdentity.FileId
        LockVolumeSerial = $script:installRootLock.LockVolumeSerial
        LockFileId = $script:installRootLock.LockFileId
        InstallerSignerId = [string]$InstallerIdentity.Id
        InstallerSignerThumbprint = [string]$InstallerIdentity.Thumbprint
        ArtifactKind = $metadata.ArtifactKind
        AutoCADSeries = $metadata.Manifest.AutoCADSeries
        AppVersion = $metadata.Manifest.AppVersionText
        ProductCode = $metadata.Manifest.ProductCode
        FileCount = [int]$metadata.Files.Count
        StageName = $StageName
        BackupName = $BackupName
        FailedName = $FailedName
        QuarantineName = $QuarantineName
        NewRootVolumeSerial = if ($null -eq $newIdentity) { '' } else { $newIdentity.RootVolumeSerial }
        NewRootFileId = if ($null -eq $newIdentity) { '' } else { $newIdentity.RootFileId }
        NewReceiptSha256 = if ($null -eq $newIdentity) { '' } else { $newIdentity.ReceiptSha256 }
        ExistingRootVolumeSerial = if ($null -eq $existingIdentity) { '' } else {
            $existingIdentity.RootVolumeSerial
        }
        ExistingRootFileId = if ($null -eq $existingIdentity) { '' } else {
            $existingIdentity.RootFileId
        }
        ExistingReceiptSha256 = if ($null -eq $existingIdentity) { '' } else {
            $existingIdentity.ReceiptSha256
        }
        NewOwnedFiles = if ($null -eq $newIdentity) {
            [Collections.Generic.List[object]]::new()
        } else { @($newIdentity.OwnedFiles) }
        NewOwnedDirectories = if ($null -eq $newIdentity) {
            [Collections.Generic.List[object]]::new()
        } else {
            @($newIdentity.OwnedDirectories)
        }
        ExistingOwnedFiles = if ($null -eq $existingIdentity) {
            [Collections.Generic.List[object]]::new()
        } else {
            @($existingIdentity.OwnedFiles)
        }
        ExistingOwnedDirectories = if ($null -eq $existingIdentity) {
            [Collections.Generic.List[object]]::new()
        } else {
            @($existingIdentity.OwnedDirectories)
        }
        HmacSha256 = ''
    }
}

function Invoke-DevelopmentTestFault {
    param([Parameter(Mandatory)][string]$Point)

    if ($DevelopmentTestFault -eq $Point) {
        [Environment]::Exit(91)
    }
}

function Invoke-DevelopmentPreCommitBarrier {
    param([Parameter(Mandatory)][string]$InstallRootPath)

    if ([string]::IsNullOrWhiteSpace($DevelopmentTestPreCommitBarrierPath)) { return }
    $barrier = Get-FullLocalPath `
        $DevelopmentTestPreCommitBarrierPath 'DEVELOPMENT_TEST_BARRIER_INVALID'
    $root = [IO.Path]::GetFullPath($InstallRootPath).TrimEnd(
        [IO.Path]::DirectorySeparatorChar)
    if (-not ([IO.Path]::GetDirectoryName($barrier)).Equals(
            $root, [StringComparison]::OrdinalIgnoreCase) -or
        [IO.Path]::GetFileName($barrier) -notmatch
            '^\.cad-harness-installer-test-barrier\.[0-9a-f]{32}$') {
        Stop-Installer 'DEVELOPMENT_TEST_BARRIER_INVALID'
    }
    $ready = "$barrier.ready"
    $release = "$barrier.release"
    try {
        $stream = [IO.FileStream]::new(
            $ready, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read)
        try { $stream.Flush($true) } finally { $stream.Dispose() }
        $deadline = [DateTime]::UtcNow.AddSeconds(10)
        while (-not [IO.File]::Exists($release)) {
            if ([DateTime]::UtcNow -ge $deadline) {
                Stop-Installer 'DEVELOPMENT_TEST_BARRIER_TIMEOUT'
            }
            Start-Sleep -Milliseconds 25
        }
    }
    finally {
        if ([IO.File]::Exists($ready)) { [IO.File]::Delete($ready) }
        if ([IO.File]::Exists($release)) { [IO.File]::Delete($release) }
    }
}

function Remove-TransactionOwnedTree {
    param(
        [Parameter(Mandatory)][string]$InstallRootPath,
        [Parameter(Mandatory)][string]$Target,
        [Parameter(Mandatory)][AllowEmptyCollection()][object[]]$OwnedFiles,
        [Parameter(Mandatory)][AllowEmptyCollection()][object[]]$OwnedDirectories
    )

    Assert-DirectOwnedChild $InstallRootPath $Target
    if (-not [IO.Directory]::Exists($Target)) { return }
    Assert-NoReparseTree $Target
    $allowedFiles = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($relative in $OwnedFiles) { $null = $allowedFiles.Add([string]$relative) }
    $allowedDirectories = [Collections.Generic.HashSet[string]]::new(
        [StringComparer]::OrdinalIgnoreCase)
    foreach ($relative in $OwnedDirectories) {
        $null = $allowedDirectories.Add([string]$relative)
    }
    foreach ($file in @(Get-SafeTreeFiles $Target 'OWNED_TREE_UNREADABLE')) {
        $relative = [IO.Path]::GetRelativePath($Target, $file.FullName).Replace('\', '/')
        if (-not $allowedFiles.Contains($relative)) {
            Stop-Installer 'OWNERSHIP_INVENTORY_MISMATCH'
        }
    }
    foreach ($relative in @(Get-RelativeDirectoryInventory $Target)) {
        if (-not $allowedDirectories.Contains([string]$relative)) {
            Stop-Installer 'OWNERSHIP_INVENTORY_MISMATCH'
        }
    }

    $deleted = 0
    foreach ($relative in @($OwnedFiles)) {
        $path = Get-SafeChildPath $Target ([string]$relative)
        if ([IO.File]::Exists($path)) {
            if (Test-ReparsePoint $path) { Stop-Installer 'REPARSE_POINT_NOT_ALLOWED' }
            Assert-NoAlternateDataStreams $path
            try { [IO.File]::Delete($path) }
            catch { Stop-Installer 'CLEANUP_PENDING' }
            $deleted++
            if ($deleted -eq 1) { Invoke-DevelopmentTestFault 'CleanupAfterOneDelete' }
        }
    }
    [string[]]$directories = @($OwnedDirectories | ForEach-Object { [string]$_ })
    [Array]::Sort($directories, [Comparison[string]]{
            param($left, $right)
            $depth = $right.Split('/').Count.CompareTo($left.Split('/').Count)
            if ($depth -ne 0) { return $depth }
            return [StringComparer]::Ordinal.Compare($right, $left)
        })
    foreach ($relative in $directories) {
        $path = Get-SafeChildPath $Target "$relative/owned-placeholder"
        $path = [IO.Path]::GetDirectoryName($path)
        if ([IO.Directory]::Exists($path)) {
            try { [IO.Directory]::Delete($path, $false) }
            catch { Stop-Installer 'CLEANUP_PENDING' }
        }
    }
    try { [IO.Directory]::Delete($Target, $false) }
    catch { Stop-Installer 'CLEANUP_PENDING' }
}

function Assert-JournalContext {
    param(
        [Parameter(Mandatory)]$Journal,
        [Parameter(Mandatory)]$InstallerIdentity
    )

    $expectedKind = if ($DevelopmentUnsigned) { 'DEVELOPMENT-UNSIGNED' } else { 'RELEASE-SIGNED' }
    if ($Journal.ArtifactKind -ne $expectedKind -or
        $Journal.AutoCADSeries -ne $ExpectedAutoCADSeries) {
        Stop-Installer 'RECOVERY_CONTEXT_MISMATCH'
    }
    if ([string]$Journal.InstallerSignerId -cne [string]$InstallerIdentity.Id -or
        [string]$Journal.InstallerSignerThumbprint -cne
            [string]$InstallerIdentity.Thumbprint) {
        Stop-Installer 'RECOVERY_INSTALLER_SIGNER_MISMATCH'
    }
    if ($Journal.ArtifactKind -eq 'RELEASE-SIGNED' -and
        [string]::IsNullOrWhiteSpace([string]$Journal.InstallerSignerThumbprint)) {
        Stop-Installer 'JOURNAL_INVALID'
    }
}

function Get-JournalExpectedIdentity {
    param(
        [Parameter(Mandatory)]$Journal,
        [Parameter(Mandatory)][ValidateSet('New', 'Existing')][string]$Kind
    )

    return [pscustomobject]@{
        RootVolumeSerial = [string]$Journal."${Kind}RootVolumeSerial"
        RootFileId = [string]$Journal."${Kind}RootFileId"
        ReceiptSha256 = [string]$Journal."${Kind}ReceiptSha256"
        OwnedFiles = @($Journal."${Kind}OwnedFiles")
        OwnedDirectories = @($Journal."${Kind}OwnedDirectories")
    }
}

function Get-RecoveryValidation {
    param(
        [Parameter(Mandatory)][string]$LiteralPath,
        [Parameter(Mandatory)]$ExpectedIdentity,
        [switch]$AllowStagingName
    )

    $validation = Get-BundleValidation `
        -Root $LiteralPath `
        -ExpectedSeries $ExpectedAutoCADSeries `
        -UnsignedDevelopment ([bool]$DevelopmentUnsigned) `
        -Installed `
        -AllowStagingName:$AllowStagingName
    Assert-ValidationIdentityUnchanged $ExpectedIdentity $validation
    return $validation
}

function Complete-PublishedRecovery {
    param(
        [Parameter(Mandatory)][string]$InstallRootPath,
        [Parameter(Mandatory)]$Journal,
        [Parameter(Mandatory)][byte[]]$Key
    )

    $destination = Join-Path $InstallRootPath $bundleName
    $newIdentity = Get-JournalExpectedIdentity $Journal 'New'
    $null = Get-RecoveryValidation $destination $newIdentity
    Set-TransactionPhase $InstallRootPath $Journal 'verified' $Key
    Set-TransactionPhase $InstallRootPath $Journal 'cleanup_pending' $Key
    if ($Journal.Action -eq 'upgrade') {
        $backup = Join-Path $InstallRootPath ([string]$Journal.BackupName)
        Remove-TransactionOwnedTree `
            $InstallRootPath $backup `
            @($Journal.ExistingOwnedFiles) @($Journal.ExistingOwnedDirectories)
    }
    $stage = Join-Path $InstallRootPath ([string]$Journal.StageName)
    if ([IO.Directory]::Exists($stage)) {
        Remove-TransactionOwnedTree `
            $InstallRootPath $stage @($Journal.NewOwnedFiles) @($Journal.NewOwnedDirectories)
    }
    Clear-TransactionJournal $InstallRootPath
    $script:RecoveredTransaction = $Journal
}

function Complete-UninstallRecovery {
    param(
        [Parameter(Mandatory)][string]$InstallRootPath,
        [Parameter(Mandatory)]$Journal,
        [Parameter(Mandatory)][byte[]]$Key
    )

    Set-TransactionPhase $InstallRootPath $Journal 'cleanup_pending' $Key
    $quarantine = Join-Path $InstallRootPath ([string]$Journal.QuarantineName)
    Remove-TransactionOwnedTree `
        $InstallRootPath $quarantine `
        @($Journal.ExistingOwnedFiles) @($Journal.ExistingOwnedDirectories)
    Clear-TransactionJournal $InstallRootPath
    $script:RecoveredTransaction = $Journal
}

function Invoke-TransactionRecovery {
    param(
        [Parameter(Mandatory)][string]$InstallRootPath,
        [Parameter(Mandatory)]$RootIdentity,
        [Parameter(Mandatory)][byte[]]$Key,
        [Parameter(Mandatory)]$InstallerIdentity
    )

    Assert-InstallRootTransactionLock $InstallRootPath
    $journal = Read-TransactionJournal $InstallRootPath $RootIdentity $Key
    if ($null -eq $journal) { return }
    Assert-JournalContext $journal $InstallerIdentity
    $destination = Join-Path $InstallRootPath $bundleName
    $stage = if ([string]::IsNullOrWhiteSpace([string]$journal.StageName)) { '' } else {
        Join-Path $InstallRootPath ([string]$journal.StageName)
    }
    $backup = if ([string]::IsNullOrWhiteSpace([string]$journal.BackupName)) { '' } else {
        Join-Path $InstallRootPath ([string]$journal.BackupName)
    }
    $quarantine = if ([string]::IsNullOrWhiteSpace([string]$journal.QuarantineName)) { '' } else {
        Join-Path $InstallRootPath ([string]$journal.QuarantineName)
    }

    if ($journal.Action -eq 'install') {
        if ($journal.Phase -eq 'prepared') {
            if ([IO.Directory]::Exists($destination) -and
                -not [IO.Directory]::Exists($stage)) {
                Complete-PublishedRecovery $InstallRootPath $journal $Key
                return
            }
            if (-not [IO.Directory]::Exists($destination) -and
                [IO.Directory]::Exists($stage)) {
                Remove-TransactionOwnedTree `
                    $InstallRootPath $stage `
                    @($journal.NewOwnedFiles) @($journal.NewOwnedDirectories)
                Clear-TransactionJournal $InstallRootPath
                return
            }
            Stop-Installer 'RECOVERY_STATE_AMBIGUOUS'
        }
        Complete-PublishedRecovery $InstallRootPath $journal $Key
        return
    }

    if ($journal.Action -eq 'upgrade') {
        if ($journal.Phase -eq 'prepared') {
            if ([IO.Directory]::Exists($destination) -and
                -not [IO.Directory]::Exists($backup) -and
                [IO.Directory]::Exists($stage)) {
                $oldIdentity = Get-JournalExpectedIdentity $journal 'Existing'
                $null = Get-RecoveryValidation $destination $oldIdentity
                Remove-TransactionOwnedTree `
                    $InstallRootPath $stage `
                    @($journal.NewOwnedFiles) @($journal.NewOwnedDirectories)
                Clear-TransactionJournal $InstallRootPath
                return
            }
            if (-not [IO.Directory]::Exists($destination) -and
                [IO.Directory]::Exists($backup) -and [IO.Directory]::Exists($stage)) {
                $oldIdentity = Get-JournalExpectedIdentity $journal 'Existing'
                $null = Get-RecoveryValidation $backup $oldIdentity -AllowStagingName
                Assert-InstallRootTransactionLock $InstallRootPath
                [IO.Directory]::Move($backup, $destination)
                Remove-TransactionOwnedTree `
                    $InstallRootPath $stage `
                    @($journal.NewOwnedFiles) @($journal.NewOwnedDirectories)
                Clear-TransactionJournal $InstallRootPath
                return
            }
            if ([IO.Directory]::Exists($destination) -and
                [IO.Directory]::Exists($backup) -and -not [IO.Directory]::Exists($stage)) {
                Complete-PublishedRecovery $InstallRootPath $journal $Key
                return
            }
            Stop-Installer 'RECOVERY_STATE_AMBIGUOUS'
        }
        if ($journal.Phase -eq 'old_quarantined') {
            if (-not [IO.Directory]::Exists($destination) -and
                [IO.Directory]::Exists($backup) -and [IO.Directory]::Exists($stage)) {
                $newIdentity = Get-JournalExpectedIdentity $journal 'New'
                $null = Get-RecoveryValidation $stage $newIdentity -AllowStagingName
                Assert-InstallRootTransactionLock $InstallRootPath
                [IO.Directory]::Move($stage, $destination)
                Set-TransactionPhase $InstallRootPath $journal 'published' $Key
            }
            elseif ([IO.Directory]::Exists($destination) -and
                [IO.Directory]::Exists($backup) -and -not [IO.Directory]::Exists($stage)) {
                Set-TransactionPhase $InstallRootPath $journal 'published' $Key
            }
            else { Stop-Installer 'RECOVERY_STATE_AMBIGUOUS' }
        }
        Complete-PublishedRecovery $InstallRootPath $journal $Key
        return
    }

    if ($journal.Phase -eq 'prepared') {
        if ([IO.Directory]::Exists($destination) -and
            -not [IO.Directory]::Exists($quarantine)) {
            Clear-TransactionJournal $InstallRootPath
            return
        }
        if (-not [IO.Directory]::Exists($destination) -and
            [IO.Directory]::Exists($quarantine)) {
            Complete-UninstallRecovery $InstallRootPath $journal $Key
            return
        }
        Stop-Installer 'RECOVERY_STATE_AMBIGUOUS'
    }
    if ($journal.Phase -eq 'verified_precommit') {
        if ([IO.Directory]::Exists($destination) -and
            -not [IO.Directory]::Exists($quarantine)) {
            $oldIdentity = Get-JournalExpectedIdentity $journal 'Existing'
            $null = Get-RecoveryValidation $destination $oldIdentity
            Assert-InstallRootTransactionLock $InstallRootPath
            [IO.Directory]::Move($destination, $quarantine)
        }
        elseif ([IO.Directory]::Exists($destination) -or
            -not [IO.Directory]::Exists($quarantine)) {
            Stop-Installer 'RECOVERY_STATE_AMBIGUOUS'
        }
    }
    if ([IO.Directory]::Exists($destination)) {
        Stop-Installer 'RECOVERY_STATE_AMBIGUOUS'
    }
    Complete-UninstallRecovery $InstallRootPath $journal $Key
}

function Initialize-InstallRoot {
    param([Parameter(Mandatory)][string]$RequestedRoot)

    $potential = Get-PotentialDirectoryIdentity $RequestedRoot 'INSTALL_ROOT_INVALID'
    $resolved = $potential.CanonicalPath
    if ([IO.Directory]::Exists($resolved)) {
        return (Get-ExistingDirectoryIdentity $resolved 'INSTALL_ROOT_INVALID').CanonicalPath
    }

    $parent = [IO.Path]::GetDirectoryName($resolved)
    Assert-ExistingDirectory $parent 'INSTALL_ROOT_PARENT_MISSING'
    $null = Get-FullLocalPath $parent 'INSTALL_ROOT_INVALID'
    try {
        [IO.Directory]::CreateDirectory($resolved) | Out-Null
    }
    catch {
        Stop-Installer 'INSTALL_ROOT_CREATE_FAILED'
    }
    if (Test-ReparsePoint $resolved) {
        Stop-Installer 'REPARSE_POINT_NOT_ALLOWED'
    }
    $identity = Get-ExistingDirectoryIdentity $resolved 'INSTALL_ROOT_INVALID'
    if ($identity.VolumeSerial -cne $potential.VolumeSerial) {
        Stop-Installer 'INSTALL_ROOT_IDENTITY_CHANGED'
    }
    return $identity.CanonicalPath
}

function Assert-DirectOwnedChild {
    param(
        [Parameter(Mandatory)][string]$InstallRootPath,
        [Parameter(Mandatory)][string]$Target
    )

    $root = [IO.Path]::GetFullPath($InstallRootPath).TrimEnd([IO.Path]::DirectorySeparatorChar)
    $full = [IO.Path]::GetFullPath($Target).TrimEnd([IO.Path]::DirectorySeparatorChar)
    if (-not ([IO.Path]::GetDirectoryName($full)).Equals(
            $root,
            [StringComparison]::OrdinalIgnoreCase)) {
        Stop-Installer 'BROAD_DELETION_FENCE'
    }
    $leaf = [IO.Path]::GetFileName($full)
    $temporaryPattern = '^\.AutoCADHarness\.bundle\.(stage|backup|failed|uninstall)\.[0-9a-f]{32}$'
    if ($leaf -match $temporaryPattern) {
        return
    }
    Stop-Installer 'BROAD_DELETION_FENCE'
}

function Remove-ExactOwnedTree {
    param(
        [Parameter(Mandatory)][string]$InstallRootPath,
        [Parameter(Mandatory)][string]$Target
    )

    Assert-DirectOwnedChild $InstallRootPath $Target
    if (-not [IO.Directory]::Exists($Target)) {
        return
    }
    Assert-NoReparseTree $Target
    try {
        Remove-Item -LiteralPath $Target -Recurse -Force
    }
    catch {
        Stop-Installer 'OWNED_TREE_REMOVE_FAILED'
    }
}

function Flush-CopiedFile {
    param([Parameter(Mandatory)][string]$LiteralPath)

    try {
        $stream = [IO.File]::Open(
            $LiteralPath,
            [IO.FileMode]::Open,
            [IO.FileAccess]::ReadWrite,
            [IO.FileShare]::Read)
        try {
            $stream.Flush($true)
        }
        finally {
            $stream.Dispose()
        }
    }
    catch {
        Stop-Installer 'STAGING_WRITE_FAILED'
    }
}

function Write-Receipt {
    param(
        [Parameter(Mandatory)][string]$StagingRoot,
        [Parameter(Mandatory)]$Validation
    )

    $files = foreach ($relative in $Validation.Checksum.Files) {
        [ordered]@{
            RelativePath = $relative
            Sha256 = $Validation.Checksum.Checksums[$relative]
        }
    }
    $receipt = [ordered]@{
        SchemaVersion = '2.0'
        Owner = 'autocad-mechanical-harness'
        BundleName = $bundleName
        ArtifactKind = $Validation.ArtifactKind
        AutoCADSeries = $Validation.Manifest.AutoCADSeries
        AppVersion = $Validation.Manifest.AppVersionText
        ProductCode = $Validation.Manifest.ProductCode
        UpgradeCode = $Validation.Manifest.UpgradeCode
        ChecksumManifestSha256 = $Validation.Checksum.Sha256
        SignerId = [string]$Validation.SignerId
        Files = @($files)
        Directories = @(Get-RelativeDirectoryInventory $StagingRoot)
    }
    $payload = ($receipt | ConvertTo-Json -Depth 6 -Compress) + "`n"
    $path = Join-Path $StagingRoot $receiptName
    try {
        $bytes = [Text.UTF8Encoding]::new($false).GetBytes($payload)
        $stream = [IO.FileStream]::new(
            $path,
            [IO.FileMode]::CreateNew,
            [IO.FileAccess]::Write,
            [IO.FileShare]::None)
        try {
            $stream.Write($bytes, 0, $bytes.Length)
            $stream.Flush($true)
        }
        finally {
            $stream.Dispose()
        }
    }
    catch {
        Stop-Installer 'INSTALL_RECEIPT_WRITE_FAILED'
    }
}

function New-StagedBundle {
    param(
        [Parameter(Mandatory)][string]$InstallRootPath,
        [Parameter(Mandatory)]$SourceValidation,
        [Parameter(Mandatory)][string]$StagePath
    )

    Assert-DirectOwnedChild $InstallRootPath $StagePath
    $created = $false
    try {
        if ([IO.Directory]::Exists($StagePath)) {
            Stop-Installer 'STAGING_PATH_COLLISION'
        }
        [IO.Directory]::CreateDirectory($StagePath) | Out-Null
        $created = $true
        foreach ($relative in $SourceValidation.Files) {
            $source = Get-SafeChildPath $SourceValidation.Root $relative
            $destination = Get-SafeChildPath $StagePath $relative
            $parent = [IO.Path]::GetDirectoryName($destination)
            [IO.Directory]::CreateDirectory($parent) | Out-Null
            [IO.File]::Copy($source, $destination, $false)
            Flush-CopiedFile $destination
        }
        Write-Receipt $StagePath $SourceValidation
        return Get-BundleValidation `
            -Root $StagePath `
            -ExpectedSeries $ExpectedAutoCADSeries `
            -UnsignedDevelopment ([bool]$DevelopmentUnsigned) `
            -Installed `
            -AllowStagingName
    }
    catch {
        if ($created -and [IO.Directory]::Exists($StagePath)) {
            Remove-ExactOwnedTree $InstallRootPath $StagePath
        }
        throw
    }
}

function Invoke-Install {
    param(
        [Parameter(Mandatory)]$SourceValidation,
        [Parameter(Mandatory)][string]$ResolvedInstallRoot,
        [Parameter(Mandatory)]$RootIdentity,
        [Parameter(Mandatory)][byte[]]$JournalKey,
        [Parameter(Mandatory)]$InstallerIdentity
    )

    Assert-AutoCADStopped $ResolvedInstallRoot
    $destination = Join-Path $ResolvedInstallRoot $bundleName
    $existing = $null
    if ([IO.File]::Exists($destination)) {
        Stop-Installer 'BUNDLE_ALREADY_INSTALLED'
    }
    if ([IO.Directory]::Exists($destination)) {
        if (-not $Upgrade) {
            Stop-Installer 'BUNDLE_ALREADY_INSTALLED'
        }
        $existing = Get-BundleValidation `
            -Root $destination `
            -ExpectedSeries $ExpectedAutoCADSeries `
            -UnsignedDevelopment ([bool]$DevelopmentUnsigned) `
            -Installed
        if ($SourceValidation.ArtifactKind -ne $existing.ArtifactKind -or
            $SourceValidation.Manifest.UpgradeCode -ne $existing.Manifest.UpgradeCode) {
            Stop-Installer 'UPGRADE_IDENTITY_MISMATCH'
        }
        if ($SourceValidation.ArtifactKind -eq 'RELEASE-SIGNED') {
            Assert-ApprovedSignerRotation $existing.SignerId $SourceValidation.Signer
        }
        if ($SourceValidation.Manifest.AppVersion -le $existing.Manifest.AppVersion) {
            Stop-Installer 'UPGRADE_VERSION_NOT_NEWER'
        }
        if ($SourceValidation.Manifest.ProductCode -eq $existing.Manifest.ProductCode) {
            Stop-Installer 'UPGRADE_PRODUCT_CODE_REUSED'
        }
    }
    elseif ($Upgrade) {
        Stop-Installer 'UPGRADE_TARGET_MISSING'
    }

    $token = [Guid]::NewGuid().ToString('N')
    $stage = Join-Path $ResolvedInstallRoot ".AutoCADHarness.bundle.stage.$token"
    $backup = Join-Path $ResolvedInstallRoot ".AutoCADHarness.bundle.backup.$token"
    $failed = Join-Path $ResolvedInstallRoot ".AutoCADHarness.bundle.failed.$token"
    $staged = New-StagedBundle $ResolvedInstallRoot $SourceValidation $stage
    $stagedIdentity = Get-ValidationIdentity $staged
    $existingIdentity = if ($null -eq $existing) { $null } else {
        Get-ValidationIdentity $existing
    }
    $transactionAction = if ($null -eq $existing) { 'install' } else { 'upgrade' }
    $journal = New-TransactionJournal `
        $transactionAction $RootIdentity $InstallerIdentity $staged $existing `
        ([IO.Path]::GetFileName($stage)) ([IO.Path]::GetFileName($backup)) `
        ([IO.Path]::GetFileName($failed)) ''
    Write-TransactionJournal $ResolvedInstallRoot $journal $JournalKey
    Invoke-DevelopmentTestFault 'InstallAfterPrepared'
    Invoke-DevelopmentPreCommitBarrier $ResolvedInstallRoot

    Assert-AutoCADStopped $ResolvedInstallRoot
    $currentRootIdentity = Get-ExistingDirectoryIdentity `
        $ResolvedInstallRoot 'INSTALL_ROOT_IDENTITY_CHANGED'
    if ($currentRootIdentity.VolumeSerial -cne $RootIdentity.VolumeSerial -or
        $currentRootIdentity.FileId -cne $RootIdentity.FileId -or
        $currentRootIdentity.CanonicalPath -cne $RootIdentity.CanonicalPath) {
        Stop-Installer 'INSTALL_ROOT_IDENTITY_CHANGED'
    }
    $stagedFresh = Get-BundleValidation `
        -Root $stage `
        -ExpectedSeries $ExpectedAutoCADSeries `
        -UnsignedDevelopment ([bool]$DevelopmentUnsigned) `
        -Installed `
        -AllowStagingName
    Assert-ValidationIdentityUnchanged $stagedIdentity $stagedFresh

    if ($null -eq $existing) {
        if ([IO.Directory]::Exists($destination) -or [IO.File]::Exists($destination)) {
            Stop-Installer 'BUNDLE_ALREADY_INSTALLED'
        }
        Assert-InstallRootTransactionLock $ResolvedInstallRoot
        [IO.Directory]::Move($stage, $destination)
        Invoke-DevelopmentTestFault 'InstallAfterPublishBeforeJournal'
    }
    else {
        $existingFresh = Get-BundleValidation `
            -Root $destination `
            -ExpectedSeries $ExpectedAutoCADSeries `
            -UnsignedDevelopment ([bool]$DevelopmentUnsigned) `
            -Installed
        Assert-ValidationIdentityUnchanged $existingIdentity $existingFresh
        Assert-InstallRootTransactionLock $ResolvedInstallRoot
        [IO.Directory]::Move($destination, $backup)
        Invoke-DevelopmentTestFault 'UpgradeAfterOldRenameBeforeJournal'
        Set-TransactionPhase $ResolvedInstallRoot $journal 'old_quarantined' $JournalKey
        Assert-InstallRootTransactionLock $ResolvedInstallRoot
        [IO.Directory]::Move($stage, $destination)
        Invoke-DevelopmentTestFault 'UpgradeAfterPublishBeforeJournal'
    }
    Set-TransactionPhase $ResolvedInstallRoot $journal 'published' $JournalKey
    $installed = Get-BundleValidation `
        -Root $destination `
        -ExpectedSeries $ExpectedAutoCADSeries `
        -UnsignedDevelopment ([bool]$DevelopmentUnsigned) `
        -Installed
    Assert-ValidationIdentityUnchanged $stagedIdentity $installed
    Set-TransactionPhase $ResolvedInstallRoot $journal 'verified' $JournalKey
    Set-TransactionPhase $ResolvedInstallRoot $journal 'cleanup_pending' $JournalKey
    if ($null -ne $existing) {
        Remove-TransactionOwnedTree `
            $ResolvedInstallRoot $backup `
            @($journal.ExistingOwnedFiles) @($journal.ExistingOwnedDirectories)
    }
    Clear-TransactionJournal $ResolvedInstallRoot
    return $installed
}

function Invoke-Uninstall {
    param(
        [Parameter(Mandatory)][string]$ResolvedInstallRoot,
        [Parameter(Mandatory)]$RootIdentity,
        [Parameter(Mandatory)][byte[]]$JournalKey,
        [Parameter(Mandatory)]$InstallerIdentity
    )

    Assert-AutoCADStopped $ResolvedInstallRoot
    if ($Upgrade) {
        Stop-Installer 'UPGRADE_NOT_VALID_FOR_UNINSTALL'
    }
    $destination = Join-Path $ResolvedInstallRoot $bundleName
    if (-not [IO.Directory]::Exists($destination)) {
        Stop-Installer 'INSTALLED_BUNDLE_NOT_FOUND'
    }
    $validation = Get-BundleValidation `
        -Root $destination `
        -ExpectedSeries $ExpectedAutoCADSeries `
        -UnsignedDevelopment ([bool]$DevelopmentUnsigned) `
        -Installed
    if ($validation.ArtifactKind -eq 'RELEASE-SIGNED') {
        Assert-ApprovedSignerRotation $validation.SignerId $InstallerIdentity
    }
    $token = [Guid]::NewGuid().ToString('N')
    $quarantine = Join-Path $ResolvedInstallRoot ".AutoCADHarness.bundle.uninstall.$token"
    Assert-DirectOwnedChild $ResolvedInstallRoot $quarantine
    $validationIdentity = Get-ValidationIdentity $validation
    $journal = New-TransactionJournal `
        'uninstall' $RootIdentity $InstallerIdentity $null $validation '' '' '' `
        ([IO.Path]::GetFileName($quarantine))
    Write-TransactionJournal $ResolvedInstallRoot $journal $JournalKey

    Assert-AutoCADStopped $ResolvedInstallRoot
    $currentRootIdentity = Get-ExistingDirectoryIdentity `
        $ResolvedInstallRoot 'INSTALL_ROOT_IDENTITY_CHANGED'
    if ($currentRootIdentity.VolumeSerial -cne $RootIdentity.VolumeSerial -or
        $currentRootIdentity.FileId -cne $RootIdentity.FileId -or
        $currentRootIdentity.CanonicalPath -cne $RootIdentity.CanonicalPath) {
        Stop-Installer 'INSTALL_ROOT_IDENTITY_CHANGED'
    }
    $validationFresh = Get-BundleValidation `
        -Root $destination `
        -ExpectedSeries $ExpectedAutoCADSeries `
        -UnsignedDevelopment ([bool]$DevelopmentUnsigned) `
        -Installed
    Assert-ValidationIdentityUnchanged $validationIdentity $validationFresh
    Set-TransactionPhase $ResolvedInstallRoot $journal 'verified_precommit' $JournalKey
    Assert-InstallRootTransactionLock $ResolvedInstallRoot
    [IO.Directory]::Move($destination, $quarantine)
    Invoke-DevelopmentTestFault 'UninstallAfterRenameBeforeJournal'
    Set-TransactionPhase $ResolvedInstallRoot $journal 'quarantined' $JournalKey
    Set-TransactionPhase $ResolvedInstallRoot $journal 'cleanup_pending' $JournalKey
    Remove-TransactionOwnedTree `
        $ResolvedInstallRoot $quarantine `
        @($journal.ExistingOwnedFiles) @($journal.ExistingOwnedDirectories)
    Clear-TransactionJournal $ResolvedInstallRoot
    return $validation
}

$script:RecoveredTransaction = $null
$script:installRootIdentity = $null
$script:installRootLock = $null
$installMutex = $null
try {
    $knownFolderRoot = Get-KnownFolderInstallRoot
    $defaultInstallRoot = (Get-PotentialDirectoryIdentity `
            $knownFolderRoot 'KNOWN_FOLDER_UNAVAILABLE').CanonicalPath
    $requestedInstallRoot = if ([string]::IsNullOrWhiteSpace($InstallRoot)) {
        $defaultInstallRoot
    }
    else { $InstallRoot }
    $resolvedInstallRoot = (Get-PotentialDirectoryIdentity `
            $requestedInstallRoot 'INSTALL_ROOT_INVALID').CanonicalPath
    Assert-DevelopmentContext $resolvedInstallRoot
    $installerSigner = if (-not $DevelopmentUnsigned -and $Action -ne 'Validate') {
        Assert-InstallerReleaseIdentity
    }
    elseif (-not [string]::IsNullOrWhiteSpace($DevelopmentTestSignaturePolicyFixture)) {
        Assert-DevelopmentSignaturePolicyFixture $DevelopmentTestSignaturePolicyFixture
    }
    else {
        [pscustomobject]@{
            Id = 'development-unsigned'
            Thumbprint = ''
            AllowedPreviousSignerIds = @()
        }
    }

    if ($Action -eq 'Validate') {
        if ([string]::IsNullOrWhiteSpace($BundlePath)) {
            Stop-Installer 'BUNDLE_PATH_REQUIRED'
        }
        if ($Upgrade -or $AllowRunningAutoCADForDevelopmentTest -or
            $DevelopmentTestFault -ne 'None' -or
            $DevelopmentTestHoldMutexMilliseconds -gt 0) {
            Stop-Installer 'VALIDATE_FLAGS_INVALID'
        }
        $validation = Get-BundleValidation `
            -Root $BundlePath `
            -ExpectedSeries $ExpectedAutoCADSeries `
            -UnsignedDevelopment ([bool]$DevelopmentUnsigned)
        if (-not $DevelopmentUnsigned) {
            $installerSigner = Assert-InstallerReleaseIdentity
            if ($validation.SignerId -cne $installerSigner.Id -or
                $validation.SignerThumbprint -cne $installerSigner.Thumbprint) {
                Stop-Installer 'INSTALLER_SIGNER_MISMATCH'
            }
        }
        $result = [ordered]@{
            ok = $true
            action = 'validated'
            artifact_kind = $validation.ArtifactKind
            autocad_series = $validation.Manifest.AutoCADSeries
            app_version = $validation.Manifest.AppVersionText
            product_code = $validation.Manifest.ProductCode
            file_count = $validation.Files.Count
            publication_status = 'not_applicable'
            verification_status = 'verified'
            cleanup_status = 'not_applicable'
            recovery_status = 'none'
        }
    }
    elseif ($Action -eq 'Install') {
        if ([string]::IsNullOrWhiteSpace($BundlePath)) {
            Stop-Installer 'BUNDLE_PATH_REQUIRED'
        }
        $resolvedInstallRoot = Initialize-InstallRoot $resolvedInstallRoot
        Assert-DevelopmentContext $resolvedInstallRoot
        $rootIdentity = Get-ExistingDirectoryIdentity `
            $resolvedInstallRoot 'INSTALL_ROOT_INVALID'
        $installMutex = Enter-InstallRootMutex $rootIdentity
        $hasRecoveryJournal = [IO.File]::Exists((Join-Path $resolvedInstallRoot $journalName))
        $null = Enter-InstallRootTransactionLock `
            $resolvedInstallRoot $rootIdentity -RequireExisting:$hasRecoveryJournal
        $journalKey = Get-JournalKey `
            $resolvedInstallRoot -RequireExisting:$hasRecoveryJournal
        Invoke-TransactionRecovery `
            $resolvedInstallRoot $rootIdentity $journalKey $installerSigner
        if ($null -ne $script:RecoveredTransaction -and
            $script:RecoveredTransaction.Action -in @('install', 'upgrade')) {
            $recovered = $script:RecoveredTransaction
            $result = [ordered]@{
                ok = $true
                action = if ($recovered.Action -eq 'upgrade') { 'upgraded' } else { 'installed' }
                artifact_kind = $recovered.ArtifactKind
                autocad_series = $recovered.AutoCADSeries
                app_version = $recovered.AppVersion
                product_code = $recovered.ProductCode
                file_count = [int]$recovered.FileCount
                publication_status = 'published'
                verification_status = 'verified'
                cleanup_status = 'complete'
                recovery_status = 'completed'
            }
        }
        else {
            Assert-AutoCADStopped $resolvedInstallRoot
            $sourceValidation = Get-BundleValidation `
                -Root $BundlePath `
                -ExpectedSeries $ExpectedAutoCADSeries `
                -UnsignedDevelopment ([bool]$DevelopmentUnsigned)
            if (-not $DevelopmentUnsigned) {
                if ($sourceValidation.SignerId -cne $installerSigner.Id -or
                    $sourceValidation.SignerThumbprint -cne $installerSigner.Thumbprint) {
                    Stop-Installer 'INSTALLER_SIGNER_MISMATCH'
                }
            }
            $installed = Invoke-Install `
                $sourceValidation $resolvedInstallRoot $rootIdentity $journalKey `
                $installerSigner
            $result = [ordered]@{
                ok = $true
                action = if ($Upgrade) { 'upgraded' } else { 'installed' }
                artifact_kind = $installed.ArtifactKind
                autocad_series = $installed.Manifest.AutoCADSeries
                app_version = $installed.Manifest.AppVersionText
                product_code = $installed.Manifest.ProductCode
                file_count = $installed.Files.Count
                publication_status = 'published'
                verification_status = 'verified'
                cleanup_status = 'complete'
                recovery_status = 'none'
            }
        }
    }
    else {
        if (-not [string]::IsNullOrWhiteSpace($BundlePath)) {
            Stop-Installer 'BUNDLE_PATH_NOT_VALID_FOR_UNINSTALL'
        }
        Assert-DevelopmentContext $resolvedInstallRoot
        Assert-AutoCADStopped $resolvedInstallRoot
        if (-not [IO.Directory]::Exists($resolvedInstallRoot)) {
            Stop-Installer 'INSTALLED_BUNDLE_NOT_FOUND'
        }
        $rootIdentity = Get-ExistingDirectoryIdentity `
            $resolvedInstallRoot 'INSTALL_ROOT_INVALID'
        $installMutex = Enter-InstallRootMutex $rootIdentity
        $hasRecoveryJournal = [IO.File]::Exists((Join-Path $resolvedInstallRoot $journalName))
        $null = Enter-InstallRootTransactionLock `
            $resolvedInstallRoot $rootIdentity -RequireExisting:$hasRecoveryJournal
        $journalKey = Get-JournalKey `
            $resolvedInstallRoot -RequireExisting:$hasRecoveryJournal
        Invoke-TransactionRecovery `
            $resolvedInstallRoot $rootIdentity $journalKey $installerSigner
        if ($null -ne $script:RecoveredTransaction -and
            $script:RecoveredTransaction.Action -eq 'uninstall') {
            $recovered = $script:RecoveredTransaction
            $result = [ordered]@{
                ok = $true
                action = 'uninstalled'
                artifact_kind = $recovered.ArtifactKind
                autocad_series = $recovered.AutoCADSeries
                app_version = $recovered.AppVersion
                product_code = $recovered.ProductCode
                file_count = [int]$recovered.FileCount
                publication_status = 'removed'
                verification_status = 'verified_before_commit'
                cleanup_status = 'complete'
                recovery_status = 'completed'
            }
        }
        else {
            $removed = Invoke-Uninstall `
                $resolvedInstallRoot $rootIdentity $journalKey $installerSigner
            $result = [ordered]@{
                ok = $true
                action = 'uninstalled'
                artifact_kind = $removed.ArtifactKind
                autocad_series = $removed.Manifest.AutoCADSeries
                app_version = $removed.Manifest.AppVersionText
                product_code = $removed.Manifest.ProductCode
                file_count = $removed.Files.Count
                publication_status = 'removed'
                verification_status = 'verified_before_commit'
                cleanup_status = 'complete'
                recovery_status = 'none'
            }
        }
    }

    [Console]::Out.WriteLine(($result | ConvertTo-Json -Compress))
}
catch {
    $errorCode = 'INTERNAL_INSTALLER_ERROR'
    if ($_.Exception.Data.Contains('CadHarnessErrorCode')) {
        $candidate = [string]$_.Exception.Data['CadHarnessErrorCode']
        if ($candidate -match '^[A-Z0-9_]+$') {
            $errorCode = $candidate
        }
    }
    [Console]::Error.WriteLine((
            [ordered]@{
                ok = $false
                error = $errorCode
            } | ConvertTo-Json -Compress))
    exit 2
}
finally {
    if ($null -ne $script:installRootLock) {
        try { $script:installRootLock.LockHandle.Dispose() } catch { }
        try { $script:installRootLock.RootHandle.Dispose() } catch { }
        $script:installRootLock = $null
        $script:installRootIdentity = $null
    }
    if ($null -ne $installMutex) {
        try { $installMutex.ReleaseMutex() } catch { }
        $installMutex.Dispose()
    }
}
