using System.Collections.Concurrent;
using System.IO.Pipes;
using System.Runtime.CompilerServices;

[assembly: InternalsVisibleTo("CadBridge.Tests")]

namespace CadBridge.Ipc;

/// <summary>
/// Runs the framed bridge protocol over secured, per-user Windows named-pipe connections.
/// </summary>
/// <remarks>
/// Each connection carries exactly one request and one response. Multiple connections are
/// accepted concurrently so a cancellation control request can terminate a request which is
/// already executing. There is deliberately no network transport or unsecured fallback.
/// </remarks>
public sealed class NamedPipeBridgeServer
{
    private const int MinimumConcurrentHandlers = 2;
    private const int MaximumConcurrentHandlers = 254;

    private readonly PipeRequestProcessor _processor;
    private readonly PipeRequestHandler _handler;
    private readonly Func<CancellationToken, ValueTask<Stream>> _acceptConnection;
    private readonly Action<Exception>? _exceptionObserver;
    private readonly SemaphoreSlim _handlerSlots;
    private readonly ConcurrentDictionary<long, Task> _activeConnections = new();
    private long _nextConnectionId;
    private int _started;

    public NamedPipeBridgeServer(
        PipeServerOptions options,
        ILocalNamedPipeFactory factory,
        PipeRequestHandler handler,
        int maxConcurrentHandlers = 8,
        Action<Exception>? exceptionObserver = null)
        : this(
            options,
            handler,
            cancellationToken => AcceptSecuredConnectionAsync(options, factory, cancellationToken),
            maxConcurrentHandlers,
            exceptionObserver)
    {
        ArgumentNullException.ThrowIfNull(factory);
    }

    internal NamedPipeBridgeServer(
        PipeServerOptions options,
        PipeRequestHandler handler,
        Func<CancellationToken, ValueTask<Stream>> acceptConnection,
        int maxConcurrentHandlers = 8,
        Action<Exception>? exceptionObserver = null)
    {
        ArgumentNullException.ThrowIfNull(options);
        ArgumentNullException.ThrowIfNull(handler);
        ArgumentNullException.ThrowIfNull(acceptConnection);

        if (maxConcurrentHandlers is < MinimumConcurrentHandlers or > MaximumConcurrentHandlers)
        {
            throw new ArgumentOutOfRangeException(
                nameof(maxConcurrentHandlers),
                $"Concurrent handlers must be between {MinimumConcurrentHandlers} and " +
                $"{MaximumConcurrentHandlers}.");
        }

        _processor = new PipeRequestProcessor(options);
        _handler = handler;
        _acceptConnection = acceptConnection;
        _exceptionObserver = exceptionObserver;
        _handlerSlots = new SemaphoreSlim(maxConcurrentHandlers, maxConcurrentHandlers);
    }

    public int ActiveConnectionCount => _activeConnections.Count;

    /// <summary>
    /// Accepts connections until cancellation, then waits for every accepted connection to close.
    /// This method is single-use so two accept loops can never expose the same endpoint.
    /// </summary>
    public async Task RunAsync(CancellationToken cancellationToken)
    {
        if (Interlocked.Exchange(ref _started, 1) != 0)
        {
            throw new InvalidOperationException("The named-pipe server can only be run once.");
        }

        while (!cancellationToken.IsCancellationRequested)
        {
            var slotAcquired = false;
            try
            {
                await _handlerSlots.WaitAsync(cancellationToken).ConfigureAwait(false);
                slotAcquired = true;

                var connection = await _acceptConnection(cancellationToken).ConfigureAwait(false)
                    ?? throw new InvalidOperationException("The pipe factory returned no connection stream.");

                var connectionId = Interlocked.Increment(ref _nextConnectionId);
                var connectionTask = HandleConnectionAsync(connection, cancellationToken);
                if (!_activeConnections.TryAdd(connectionId, connectionTask))
                {
                    await connection.DisposeAsync().ConfigureAwait(false);
                    throw new InvalidOperationException("The pipe connection could not be tracked.");
                }

                slotAcquired = false;
                _ = connectionTask.ContinueWith(
                    static (completedTask, state) =>
                    {
                        var tracked = ((NamedPipeBridgeServer Server, long Id))state!;
                        tracked.Server._activeConnections.TryRemove(tracked.Id, out var removedTask);
                        _ = completedTask;
                        _ = removedTask;
                    },
                    (this, connectionId),
                    CancellationToken.None,
                    TaskContinuationOptions.ExecuteSynchronously,
                    TaskScheduler.Default);
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                if (slotAcquired)
                {
                    _handlerSlots.Release();
                }

                break;
            }
            catch (Exception exception)
            {
                if (slotAcquired)
                {
                    _handlerSlots.Release();
                }

                Report(exception);
                try
                {
                    await Task.Delay(TimeSpan.FromMilliseconds(10), cancellationToken)
                        .ConfigureAwait(false);
                }
                catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
                {
                    break;
                }
            }
        }

        await DrainConnectionsAsync().ConfigureAwait(false);
    }

    private static async ValueTask<Stream> AcceptSecuredConnectionAsync(
        PipeServerOptions options,
        ILocalNamedPipeFactory factory,
        CancellationToken cancellationToken)
    {
        // CreateSecuredServer is the only production entry point: it validates the per-user
        // endpoint and lets WindowsNamedPipeFactory apply CurrentUserOnly before listening.
        var stream = options.CreateSecuredServer(factory);
        try
        {
            if (stream is not NamedPipeServerStream pipe)
            {
                throw new InvalidOperationException(
                    "The secured pipe factory must return a NamedPipeServerStream.");
            }

            await pipe.WaitForConnectionAsync(cancellationToken).ConfigureAwait(false);
            return pipe;
        }
        catch
        {
            await stream.DisposeAsync().ConfigureAwait(false);
            throw;
        }
    }

    private async Task HandleConnectionAsync(Stream connection, CancellationToken cancellationToken)
    {
        try
        {
            var response = await _processor.ProcessNextAsync(
                connection,
                _handler,
                cancellationToken).ConfigureAwait(false);
            await connection.WriteAsync(response, cancellationToken).ConfigureAwait(false);
            await connection.FlushAsync(cancellationToken).ConfigureAwait(false);
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            // Shutdown owns this cancellation. Closing the stream below releases its pipe handle.
        }
        catch (Exception exception)
        {
            Report(exception);
        }
        finally
        {
            try
            {
                await connection.DisposeAsync().ConfigureAwait(false);
            }
            catch (Exception exception)
            {
                Report(exception);
            }

            _handlerSlots.Release();
        }
    }

    private async Task DrainConnectionsAsync()
    {
        while (!_activeConnections.IsEmpty)
        {
            var active = _activeConnections.Values.ToArray();
            if (active.Length == 0)
            {
                continue;
            }

            await Task.WhenAll(active).ConfigureAwait(false);
        }
    }

    private void Report(Exception exception)
    {
        if (_exceptionObserver is null)
        {
            return;
        }

        try
        {
            _exceptionObserver(exception);
        }
        catch
        {
            // Diagnostics must never tear down the listener or create an unobserved task.
        }
    }
}
