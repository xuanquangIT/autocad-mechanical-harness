using System.Security.Principal;
using Autodesk.AutoCAD.Runtime;
using CadBridge.Hosting;
using CadBridge.Ipc;
using AcApplication = Autodesk.AutoCAD.ApplicationServices.Core.Application;
using SystemException = System.Exception;

[assembly: ExtensionApplication(typeof(CadBridge.Plugin.BridgeExtensionApplication))]
[assembly: CommandClass(typeof(CadBridge.Plugin.BridgeExtensionApplication))]

namespace CadBridge.Plugin;

/// <summary>
/// Creates the real AutoCAD-bound bridge host. A module initializer in the host composition file
/// must register exactly one factory before AutoCAD calls the extension entrypoint.
/// </summary>
internal interface IAutoCadBridgeHostFactory
{
    IBridgeHost CreateHost();
}

/// <summary>Owns the secured named-pipe bridge for one loaded AutoCAD process.</summary>
public sealed class BridgeExtensionApplication : IExtensionApplication
{
    // Defaults must remain identical to config/base.yaml. User-level environment overrides are
    // consumed by both the Python process and this AutoCAD process.
    private const string DefaultPipeNameTemplate = "cadharness.{user_sid}";
    private const int DefaultMaxRequestBytes = 1_048_576;
    private const int DefaultMaxRequestDepth = 32;
    private static readonly TimeSpan ShutdownDrainTimeout = TimeSpan.FromSeconds(5);

    private static readonly object Gate = new();
    private static IAutoCadBridgeHostFactory? _hostFactory = new AutoCadBridgeHostFactory();
    private static IBridgeHost? _host;
    private static NamedPipeBridgeServer? _server;
    private static CancellationTokenSource? _shutdown;
    private static Task? _serverTask;
    private static BridgeLifecycleState _state;
    private static string? _pipeName;
    private static string? _lastFailure;

    /// <summary>Registers the production host composition before extension initialization.</summary>
    internal static void ConfigureHostFactory(IAutoCadBridgeHostFactory factory)
    {
        ArgumentNullException.ThrowIfNull(factory);
        lock (Gate)
        {
            if (_state is BridgeLifecycleState.Starting or BridgeLifecycleState.Running or
                BridgeLifecycleState.Stopping)
            {
                throw new InvalidOperationException(
                    "The AutoCAD bridge host factory cannot change while the server is active.");
            }

            _hostFactory = factory;
        }
    }

    public void Initialize()
    {
        try
        {
            StartServer();
            WriteEditorMessage("CAD Harness Bridge initialized.");
        }
        catch (SystemException exception)
        {
            RecordTerminalFailure("initialization", exception);
            WriteEditorMessage(
                "CAD Harness Bridge could not initialize. Run CADHARNESSSTATUS for diagnostics.");
        }
    }

    public void Terminate()
    {
        try
        {
            StopServer();
        }
        catch (SystemException exception)
        {
            // AutoCAD unload must never receive a bridge exception.
            RecordTerminalFailure("shutdown", exception);
        }
    }

    [CommandMethod("CADHARNESSSTATUS", CommandFlags.Modal | CommandFlags.NoHistory)]
    public void ShowStatus()
    {
        BridgeLifecycleState state;
        string pipeName;
        string lastFailure;
        int activeConnections;
        lock (Gate)
        {
            state = _state;
            pipeName = _pipeName ?? "not-created";
            lastFailure = _lastFailure ?? "none";
            activeConnections = _server?.ActiveConnectionCount ?? 0;
        }

        WriteEditorMessage(
            $"CAD Harness Bridge: state={state}; pipe={pipeName}; " +
            $"active_connections={activeConnections}; last_failure={lastFailure}.");
    }

    private static void StartServer()
    {
        lock (Gate)
        {
            if (_state is BridgeLifecycleState.Starting or BridgeLifecycleState.Running)
            {
                return;
            }

            if (_state == BridgeLifecycleState.Stopping)
            {
                throw new InvalidOperationException("The AutoCAD bridge is still stopping.");
            }

            _state = BridgeLifecycleState.Starting;
            IBridgeHost? host = null;
            CancellationTokenSource? shutdown = null;
            try
            {
                var factory = _hostFactory ?? throw new InvalidOperationException(
                    "No production AutoCAD bridge host factory was registered.");
                var sid = GetCurrentUserSid();
                var options = new PipeServerOptions
                {
                    PipeNameTemplate = Environment.GetEnvironmentVariable(
                        "CAD_HARNESS_BRIDGE_PIPE_NAME_TEMPLATE") ?? DefaultPipeNameTemplate,
                    UserSid = sid,
                    MaxRequestBytes = ReadBoundedIntegerEnvironmentVariable(
                        "CAD_HARNESS_BRIDGE_MAX_REQUEST_BYTES",
                        DefaultMaxRequestBytes,
                        4,
                        16_777_216),
                    MaxRequestDepth = ReadBoundedIntegerEnvironmentVariable(
                        "CAD_HARNESS_BRIDGE_MAX_REQUEST_DEPTH",
                        DefaultMaxRequestDepth,
                        1,
                        128),
                };
                var endpoint = options.CreateEndpoint();
                host = factory.CreateHost()
                    ?? throw new InvalidOperationException("The AutoCAD bridge host factory returned no host.");
                var router = new BridgeRequestRouter(host);
                var server = new NamedPipeBridgeServer(
                    options,
                    new WindowsNamedPipeFactory(),
                    router.Handler,
                    exceptionObserver: ObserveBackgroundFailure);
                shutdown = new CancellationTokenSource();

                _host = host;
                _server = server;
                _shutdown = shutdown;
                _pipeName = endpoint.PipeName;
                _lastFailure = null;
                _state = BridgeLifecycleState.Running;
                _serverTask = Task.Run(
                    () => RunServerAsync(server, shutdown.Token),
                    CancellationToken.None);
            }
            catch
            {
                shutdown?.Dispose();
                try
                {
                    DisposeHost(host);
                }
                catch (SystemException cleanupException)
                {
                    ObserveBackgroundFailure(cleanupException);
                }

                _host = null;
                _server = null;
                _shutdown = null;
                _serverTask = null;
                _pipeName = null;
                _state = BridgeLifecycleState.Faulted;
                throw;
            }
        }
    }

    private static async Task RunServerAsync(
        NamedPipeBridgeServer server,
        CancellationToken cancellationToken)
    {
        try
        {
            await server.RunAsync(cancellationToken).ConfigureAwait(false);
            if (!cancellationToken.IsCancellationRequested)
            {
                RecordTerminalFailure(
                    "listener",
                    new InvalidOperationException("The named-pipe listener stopped unexpectedly."));
            }
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            // The extension owns this terminal cancellation.
        }
        catch (SystemException exception)
        {
            RecordTerminalFailure("listener", exception);
        }
    }

    private static void StopServer()
    {
        CancellationTokenSource? shutdown;
        Task? serverTask;
        IBridgeHost? host;
        lock (Gate)
        {
            if (_state is BridgeLifecycleState.NotStarted or BridgeLifecycleState.Stopped)
            {
                _state = BridgeLifecycleState.Stopped;
                return;
            }

            _state = BridgeLifecycleState.Stopping;
            shutdown = _shutdown;
            serverTask = _serverTask;
            host = _host;
        }

        SystemException? shutdownFailure = null;
        var drained = true;
        try
        {
            shutdown?.Cancel();
            if (serverTask is not null)
            {
                serverTask.WaitAsync(ShutdownDrainTimeout).GetAwaiter().GetResult();
            }
        }
        catch (SystemException exception)
        {
            shutdownFailure = exception;
            drained = serverTask?.IsCompleted ?? true;
        }

        if (drained)
        {
            try
            {
                DisposeHost(host);
            }
            catch (SystemException exception)
            {
                shutdownFailure ??= exception;
            }
        }
        else if (serverTask is not null)
        {
            _ = serverTask.ContinueWith(
                static (_, state) =>
                {
                    try
                    {
                        DisposeHost((IBridgeHost?)state);
                    }
                    catch (SystemException exception)
                    {
                        ObserveBackgroundFailure(exception);
                    }
                },
                host,
                CancellationToken.None,
                TaskContinuationOptions.ExecuteSynchronously,
                TaskScheduler.Default);
        }

        try
        {
            shutdown?.Dispose();
            lock (Gate)
            {
                _host = null;
                _server = null;
                _shutdown = null;
                _serverTask = null;
                _pipeName = null;
                _state = BridgeLifecycleState.Stopped;
            }
        }
        catch (SystemException exception)
        {
            shutdownFailure ??= exception;
        }

        if (shutdownFailure is not null)
        {
            throw shutdownFailure;
        }
    }

    private static string GetCurrentUserSid()
    {
        using var identity = WindowsIdentity.GetCurrent();
        return identity.User?.Value
            ?? throw new InvalidOperationException("The current Windows identity has no user SID.");
    }

    private static int ReadBoundedIntegerEnvironmentVariable(
        string name,
        int fallback,
        int minimum,
        int maximum)
    {
        var raw = Environment.GetEnvironmentVariable(name);
        if (raw is null)
        {
            return fallback;
        }

        if (!int.TryParse(raw, out var value) || value < minimum || value > maximum)
        {
            throw new InvalidOperationException($"Environment setting {name} is invalid.");
        }

        return value;
    }

    private static void DisposeHost(IBridgeHost? host)
    {
        switch (host)
        {
            case IAsyncDisposable asyncDisposable:
                asyncDisposable.DisposeAsync().AsTask().GetAwaiter().GetResult();
                break;
            case IDisposable disposable:
                disposable.Dispose();
                break;
        }
    }

    private static void ObserveBackgroundFailure(SystemException exception)
    {
        try
        {
            ArgumentNullException.ThrowIfNull(exception);
            lock (Gate)
            {
                _lastFailure = $"background:{exception.GetType().Name}";
            }
        }
        catch
        {
            // Diagnostics must never fault the named-pipe accept loop.
        }
    }

    private static void RecordTerminalFailure(string phase, SystemException exception)
    {
        try
        {
            ArgumentException.ThrowIfNullOrWhiteSpace(phase);
            ArgumentNullException.ThrowIfNull(exception);
            lock (Gate)
            {
                _lastFailure = $"{phase}:{exception.GetType().Name}";
                if (_state != BridgeLifecycleState.Stopping)
                {
                    _state = BridgeLifecycleState.Faulted;
                }
            }
        }
        catch
        {
            // The AutoCAD extension and listener boundaries must remain exception-safe.
        }
    }

    private static void WriteEditorMessage(string message)
    {
        try
        {
            AcApplication.DocumentManager.MdiActiveDocument?.Editor.WriteMessage($"\n{message}");
        }
        catch
        {
            // AutoCAD can close the active editor while the extension is loading or unloading.
        }
    }

    private enum BridgeLifecycleState
    {
        NotStarted,
        Starting,
        Running,
        Stopping,
        Stopped,
        Faulted,
    }
}
