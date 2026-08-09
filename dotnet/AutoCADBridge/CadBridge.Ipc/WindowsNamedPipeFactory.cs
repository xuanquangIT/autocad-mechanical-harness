using System.IO.Pipes;
using System.Runtime.Versioning;
using System.Security.Principal;

namespace CadBridge.Ipc;

/// <summary>
/// Creates a byte-oriented Windows named-pipe listener that is accessible only to the
/// Windows identity named by <see cref="PipeEndpoint.AllowedUserSid"/>.
/// </summary>
/// <remarks>
/// The endpoint SID must be the identity running this server. This invariant lets the factory use
/// <see cref="PipeOptions.CurrentUserOnly"/> as an operating-system-level connection check in
/// addition to the protected ACL. Returned streams support cancellable asynchronous I/O; disposing
/// the stream closes the pipe handle and releases any pending I/O without an insecure fallback.
/// </remarks>
[SupportedOSPlatform("windows")]
public sealed class WindowsNamedPipeFactory : ILocalNamedPipeFactory
{
    private const int BufferSize = 65_536;
    private readonly SecurityIdentifier _currentUserSid;

    public WindowsNamedPipeFactory()
    {
        if (!OperatingSystem.IsWindows())
        {
            throw new PlatformNotSupportedException(
                "The secured named-pipe factory is available only on Windows.");
        }

        using var identity = WindowsIdentity.GetCurrent();
        _currentUserSid = identity.User
            ?? throw new InvalidOperationException("The current Windows identity has no user SID.");
    }

    public Stream CreateServer(PipeEndpoint endpoint)
    {
        ArgumentNullException.ThrowIfNull(endpoint);

        if (!OperatingSystem.IsWindows())
        {
            throw new PlatformNotSupportedException(
                "The secured named-pipe factory is available only on Windows.");
        }

        ValidatePipeName(endpoint.PipeName);
        var allowedSid = ParseCanonicalSid(endpoint.AllowedUserSid);
        if (!_currentUserSid.Equals(allowedSid))
        {
            throw new InvalidOperationException(
                "AllowedUserSid must exactly match the Windows identity running the pipe server.");
        }

        if (!endpoint.PipeName.Contains(allowedSid.Value, StringComparison.Ordinal))
        {
            throw new ArgumentException(
                "The pipe name must contain the exact per-user SID.",
                nameof(endpoint));
        }

        // On .NET 8, CurrentUserOnly creates a protected security descriptor whose sole owner and
        // allowed identity is the current Windows user. Passing a second PipeSecurity is forbidden
        // with that option, so the exact-SID check above is what binds endpoint policy to that ACL.
        return NamedPipeServerStreamAcl.Create(
            endpoint.PipeName,
            PipeDirection.InOut,
            // A deadline uses a second authenticated connection to cancel an active
            // request and wait for its terminal acknowledgement.
            maxNumberOfServerInstances: NamedPipeServerStream.MaxAllowedServerInstances,
            PipeTransmissionMode.Byte,
            PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly,
            inBufferSize: BufferSize,
            outBufferSize: BufferSize,
            pipeSecurity: null,
            HandleInheritability.None,
            additionalAccessRights: (PipeAccessRights)0);
    }

    private static SecurityIdentifier ParseCanonicalSid(string value)
    {
        if (string.IsNullOrWhiteSpace(value))
        {
            throw new ArgumentException("AllowedUserSid is required.", nameof(value));
        }

        SecurityIdentifier sid;
        try
        {
            sid = new SecurityIdentifier(value);
        }
        catch (ArgumentException exception)
        {
            throw new ArgumentException(
                "AllowedUserSid must be a valid canonical Windows SID.",
                nameof(value),
                exception);
        }

        if (!string.Equals(value, sid.Value, StringComparison.Ordinal))
        {
            throw new ArgumentException(
                "AllowedUserSid must be in canonical Windows SID form.",
                nameof(value));
        }

        return sid;
    }

    private static void ValidatePipeName(string pipeName)
    {
        if (string.IsNullOrWhiteSpace(pipeName) ||
            pipeName.Length > 256 ||
            pipeName.IndexOfAny(['\\', '/', ':']) >= 0 ||
            pipeName.Any(character =>
                !(char.IsAsciiLetterOrDigit(character) || character is '.' or '_' or '-')) ||
            string.Equals(pipeName, "anonymous", StringComparison.OrdinalIgnoreCase))
        {
            throw new ArgumentException(
                "PipeName must be a safe local named-pipe identifier.",
                nameof(pipeName));
        }
    }
}
