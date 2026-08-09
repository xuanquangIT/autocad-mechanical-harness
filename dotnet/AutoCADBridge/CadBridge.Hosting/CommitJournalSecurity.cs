using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Cryptography;
using System.Security.Principal;

namespace CadBridge.Hosting;

internal static class CommitJournalSecurity
{
    private const int CryptProtectUiForbidden = 0x1;
    private const string IntegrityKeyFileName = ".integrity-key";

    public static void PreparePrivateRoot(string root)
    {
        if (Directory.Exists(root) && IsReparsePoint(root))
        {
            throw new InvalidDataException("The commit journal root must not be a reparse point.");
        }

        Directory.CreateDirectory(root);
        if (IsReparsePoint(root))
        {
            throw new InvalidDataException("The commit journal root must not be a reparse point.");
        }

        if (OperatingSystem.IsWindows())
        {
            var identity = WindowsIdentity.GetCurrent().User
                ?? throw new InvalidOperationException("The current Windows user has no SID.");
            var security = new DirectorySecurity();
            security.SetAccessRuleProtection(isProtected: true, preserveInheritance: false);
            security.AddAccessRule(new FileSystemAccessRule(
                identity,
                FileSystemRights.FullControl,
                InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit,
                PropagationFlags.None,
                AccessControlType.Allow));
            new DirectoryInfo(root).SetAccessControl(security);
        }
        else
        {
            File.SetUnixFileMode(
                root,
                UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
        }
    }

    public static byte[] LoadOrCreateIntegrityKey(string root, bool journalHasEntries)
    {
        var path = Path.Combine(root, IntegrityKeyFileName);
        if (File.Exists(path))
        {
            RejectReparsePoint(path);
            var protectedKey = File.ReadAllBytes(path);
            if (protectedKey.Length is <= 0 or > 4096)
            {
                throw new InvalidDataException("The commit journal integrity key is invalid.");
            }

            var key = OperatingSystem.IsWindows()
                ? UnprotectForCurrentUser(protectedKey)
                : protectedKey;
            if (key.Length != 32)
            {
                CryptographicOperations.ZeroMemory(key);
                throw new InvalidDataException("The commit journal integrity key is invalid.");
            }

            return key;
        }

        if (journalHasEntries)
        {
            throw new InvalidDataException(
                "The commit journal integrity key is missing for existing entries.");
        }

        var newKey = RandomNumberGenerator.GetBytes(32);
        try
        {
            var stored = OperatingSystem.IsWindows()
                ? ProtectForCurrentUser(newKey)
                : newKey.ToArray();
            WriteNewDurableFile(path, stored);
            if (!OperatingSystem.IsWindows())
            {
                File.SetUnixFileMode(path, UnixFileMode.UserRead | UnixFileMode.UserWrite);
            }

            return newKey;
        }
        catch
        {
            CryptographicOperations.ZeroMemory(newKey);
            throw;
        }
    }

    public static void RejectReparsePoint(string path)
    {
        if (IsReparsePoint(path))
        {
            throw new InvalidDataException("A commit journal file must not be a reparse point.");
        }
    }

    private static bool IsReparsePoint(string path) =>
        (File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0;

    private static void WriteNewDurableFile(string path, byte[] payload)
    {
        using var stream = new FileStream(
            path,
            FileMode.CreateNew,
            FileAccess.Write,
            FileShare.None,
            bufferSize: 4096,
            FileOptions.WriteThrough);
        stream.Write(payload);
        stream.Flush(flushToDisk: true);
    }

    private static byte[] ProtectForCurrentUser(byte[] value) =>
        TransformWithDataProtectionApi(value, protect: true);

    private static byte[] UnprotectForCurrentUser(byte[] value) =>
        TransformWithDataProtectionApi(value, protect: false);

    private static byte[] TransformWithDataProtectionApi(byte[] value, bool protect)
    {
        var inputPointer = Marshal.AllocHGlobal(value.Length);
        try
        {
            Marshal.Copy(value, 0, inputPointer, value.Length);
            var input = new DataBlob(value.Length, inputPointer);
            DataBlob output;
            var succeeded = protect
                ? CryptProtectData(
                    ref input,
                    null,
                    IntPtr.Zero,
                    IntPtr.Zero,
                    IntPtr.Zero,
                    CryptProtectUiForbidden,
                    out output)
                : CryptUnprotectData(
                    ref input,
                    IntPtr.Zero,
                    IntPtr.Zero,
                    IntPtr.Zero,
                    IntPtr.Zero,
                    CryptProtectUiForbidden,
                    out output);
            if (!succeeded)
            {
                throw new InvalidDataException(
                    "Windows could not protect the commit journal integrity key.");
            }

            try
            {
                var result = new byte[output.Length];
                Marshal.Copy(output.Pointer, result, 0, output.Length);
                return result;
            }
            finally
            {
                LocalFree(output.Pointer);
            }
        }
        finally
        {
            Marshal.FreeHGlobal(inputPointer);
        }
    }

    [StructLayout(LayoutKind.Sequential)]
    private readonly struct DataBlob
    {
        public DataBlob(int length, IntPtr pointer)
        {
            Length = length;
            Pointer = pointer;
        }

        public int Length { get; }

        public IntPtr Pointer { get; }
    }

    [DllImport("crypt32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CryptProtectData(
        ref DataBlob dataIn,
        string? dataDescription,
        IntPtr optionalEntropy,
        IntPtr reserved,
        IntPtr promptStructure,
        int flags,
        out DataBlob dataOut);

    [DllImport("crypt32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CryptUnprotectData(
        ref DataBlob dataIn,
        IntPtr dataDescription,
        IntPtr optionalEntropy,
        IntPtr reserved,
        IntPtr promptStructure,
        int flags,
        out DataBlob dataOut);

    [DllImport("kernel32.dll")]
    private static extern IntPtr LocalFree(IntPtr memory);
}
