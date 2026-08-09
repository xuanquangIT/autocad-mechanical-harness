using System.Buffers.Binary;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Threading.Channels;
using CadBridge.Ipc;
using Xunit;

namespace CadBridge.Tests;

public sealed class NamedPipeBridgeServerTests
{
    [Fact]
    public async Task ConcurrentCancelTerminatesTargetAndShutdownDrainsConnections()
    {
        var connections = Channel.CreateUnbounded<ScriptedDuplexStream>();
        var targetEntered = NewSignal();
        var activeHandlers = 0;

        async ValueTask<PipeHandlerResult> Handler(
            JsonElement request,
            CancellationToken cancellationToken)
        {
            Interlocked.Increment(ref activeHandlers);
            try
            {
                if (request.GetProperty("request_id").GetString() == "target-1")
                {
                    targetEntered.TrySetResult();
                    await Task.Delay(Timeout.InfiniteTimeSpan, cancellationToken);
                }

                return PipeHandlerResult.Ok(new JsonObject { ["handled"] = true });
            }
            finally
            {
                Interlocked.Decrement(ref activeHandlers);
            }
        }

        var server = new NamedPipeBridgeServer(
            CreateOptions(),
            Handler,
            async cancellationToken => await connections.Reader.ReadAsync(cancellationToken),
            maxConcurrentHandlers: 2);
        using var shutdown = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        var running = server.RunAsync(shutdown.Token);

        var target = new ScriptedDuplexStream(Frame(ValidPayload("target-1")));
        Assert.True(connections.Writer.TryWrite(target));
        await targetEntered.Task.WaitAsync(TimeSpan.FromSeconds(2));

        var cancel = new ScriptedDuplexStream(Frame(CancelPayload("cancel-1", "target-1")));
        Assert.True(connections.Writer.TryWrite(cancel));

        await Task.WhenAll(target.Closed, cancel.Closed).WaitAsync(TimeSpan.FromSeconds(2));
        var targetResponse = ParseFrame(target.WrittenBytes);
        Assert.Equal("failed", Text(targetResponse, "status"));
        Assert.Equal("IPC_TIMEOUT", ErrorText(targetResponse, "code"));

        var cancelResponse = ParseFrame(cancel.WrittenBytes);
        Assert.Equal("ok", Text(cancelResponse, "status"));
        Assert.Equal(
            "target-1",
            cancelResponse["data"]!["cancelled_request_id"]!.GetValue<string>());
        Assert.True(cancelResponse["data"]!["terminal"]!.GetValue<bool>());

        shutdown.Cancel();
        await running.WaitAsync(TimeSpan.FromSeconds(2));

        Assert.Equal(0, activeHandlers);
        Assert.Equal(0, server.ActiveConnectionCount);
        Assert.True(target.IsDisposed);
        Assert.True(cancel.IsDisposed);
    }

    [Fact]
    public async Task AcceptAndConnectionFailuresAreContainedBeforeNextRequest()
    {
        var connections = Channel.CreateUnbounded<ScriptedDuplexStream>();
        var acceptedCalls = 0;
        var observedExceptions = new List<Exception>();

        async ValueTask<Stream> Accept(CancellationToken cancellationToken)
        {
            if (Interlocked.Increment(ref acceptedCalls) == 1)
            {
                throw new IOException("simulated accept failure");
            }

            return await connections.Reader.ReadAsync(cancellationToken);
        }

        var server = new NamedPipeBridgeServer(
            CreateOptions(),
            (_, _) => ValueTask.FromResult(PipeHandlerResult.Ok(new JsonObject())),
            Accept,
            maxConcurrentHandlers: 2,
            exceptionObserver: observedExceptions.Add);
        using var shutdown = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        var running = server.RunAsync(shutdown.Token);

        var broken = new ScriptedDuplexStream(Frame(ValidPayload("broken-1")), failWrites: true);
        var valid = new ScriptedDuplexStream(Frame(ValidPayload("valid-1")));
        Assert.True(connections.Writer.TryWrite(broken));
        Assert.True(connections.Writer.TryWrite(valid));

        await Task.WhenAll(broken.Closed, valid.Closed).WaitAsync(TimeSpan.FromSeconds(2));
        Assert.Equal("ok", Text(ParseFrame(valid.WrittenBytes), "status"));

        shutdown.Cancel();
        await running.WaitAsync(TimeSpan.FromSeconds(2));

        Assert.Contains(observedExceptions, exception => exception is IOException);
        Assert.Equal(0, server.ActiveConnectionCount);
    }

    private static PipeServerOptions CreateOptions() =>
        new()
        {
            PipeNameTemplate = "cad-harness-{user_sid}",
            UserSid = "S-1-5-21-1000",
        };

    private static byte[] ValidPayload(string requestId) => Encoding.UTF8.GetBytes(
        $"{{\"schema_version\":\"1.10\",\"method\":\"status\",\"request_id\":\"{requestId}\",\"params\":{{}}}}");

    private static byte[] CancelPayload(string requestId, string targetRequestId) => Encoding.UTF8.GetBytes(
        $"{{\"schema_version\":\"1.10\",\"method\":\"cancel\",\"request_id\":\"{requestId}\"," +
        $"\"params\":{{\"target_request_id\":\"{targetRequestId}\"}}}}");

    private static byte[] Frame(byte[] payload)
    {
        var frame = new byte[4 + payload.Length];
        BinaryPrimitives.WriteInt32BigEndian(frame.AsSpan(0, 4), payload.Length);
        payload.CopyTo(frame.AsSpan(4));
        return frame;
    }

    private static JsonObject ParseFrame(byte[] frame)
    {
        Assert.True(frame.Length >= sizeof(int));
        var declaredLength = BinaryPrimitives.ReadInt32BigEndian(frame.AsSpan(0, 4));
        Assert.Equal(frame.Length - 4, declaredLength);
        return JsonNode.Parse(frame.AsSpan(4))!.AsObject();
    }

    private static TaskCompletionSource NewSignal() =>
        new(TaskCreationOptions.RunContinuationsAsynchronously);

    private static string Text(JsonObject response, string propertyName) =>
        response[propertyName]!.GetValue<string>();

    private static string ErrorText(JsonObject response, string propertyName) =>
        response["error"]![propertyName]!.GetValue<string>();

    private sealed class ScriptedDuplexStream : Stream
    {
        private readonly MemoryStream _input;
        private readonly MemoryStream _output = new();
        private readonly bool _failWrites;
        private readonly TaskCompletionSource _closed = NewSignal();
        private int _disposed;

        public ScriptedDuplexStream(byte[] input, bool failWrites = false)
        {
            _input = new MemoryStream(input, writable: false);
            _failWrites = failWrites;
        }

        public Task Closed => _closed.Task;

        public bool IsDisposed => Volatile.Read(ref _disposed) != 0;

        public byte[] WrittenBytes => _output.ToArray();

        public override bool CanRead => true;

        public override bool CanSeek => false;

        public override bool CanWrite => true;

        public override long Length => throw new NotSupportedException();

        public override long Position
        {
            get => throw new NotSupportedException();
            set => throw new NotSupportedException();
        }

        public override void Flush()
        {
        }

        public override Task FlushAsync(CancellationToken cancellationToken) =>
            Task.CompletedTask;

        public override int Read(byte[] buffer, int offset, int count) =>
            _input.Read(buffer, offset, count);

        public override ValueTask<int> ReadAsync(
            Memory<byte> buffer,
            CancellationToken cancellationToken = default) =>
            _input.ReadAsync(buffer, cancellationToken);

        public override long Seek(long offset, SeekOrigin origin) =>
            throw new NotSupportedException();

        public override void SetLength(long value) =>
            throw new NotSupportedException();

        public override void Write(byte[] buffer, int offset, int count)
        {
            if (_failWrites)
            {
                throw new IOException("simulated connection write failure");
            }

            _output.Write(buffer, offset, count);
        }

        public override ValueTask WriteAsync(
            ReadOnlyMemory<byte> buffer,
            CancellationToken cancellationToken = default)
        {
            if (_failWrites)
            {
                throw new IOException("simulated connection write failure");
            }

            return _output.WriteAsync(buffer, cancellationToken);
        }

        protected override void Dispose(bool disposing)
        {
            if (Interlocked.Exchange(ref _disposed, 1) == 0)
            {
                _closed.TrySetResult();
            }

            base.Dispose(disposing);
        }

        public override ValueTask DisposeAsync()
        {
            Dispose(true);
            GC.SuppressFinalize(this);
            return ValueTask.CompletedTask;
        }
    }
}
