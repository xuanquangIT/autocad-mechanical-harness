using System.Buffers.Binary;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using CadBridge.Contracts;
using CadBridge.Ipc;
using Xunit;

namespace CadBridge.Tests;

public sealed class PipeRequestProcessorTests
{
    [Fact]
    public async Task ValidEnvelopeInvokesHandlerExactlyOnce()
    {
        var processor = CreateProcessor();
        var handlerCalls = 0;

        var response = await processor.ProcessPayloadAsync(
            ValidPayload("valid-1"),
            (request, _) =>
            {
                Interlocked.Increment(ref handlerCalls);
                Assert.Equal("valid-1", request.GetProperty("request_id").GetString());
                return ValueTask.FromResult(
                    PipeHandlerResult.Ok(new JsonObject { ["accepted"] = true }));
            });

        Assert.Equal(1, handlerCalls);
        Assert.Equal("ok", Text(response, "status"));
        Assert.Equal("valid-1", Text(response, "request_id"));
        Assert.True(response["data"]!["accepted"]!.GetValue<bool>());
    }

    public static TheoryData<byte[], string> RejectedPayloads => new()
    {
        { Encoding.UTF8.GetBytes("{"), "INVALID_FEATURE_PARAMETERS" },
        {
            Encoding.UTF8.GetBytes(
                """{"schema_version":"1.12","method":"does_not_exist","request_id":"unknown-1","params":{}}"""),
            "INVALID_FEATURE_PARAMETERS"
        },
        {
            Encoding.UTF8.GetBytes(
                """{"schema_version":"1.12","method":"status","request_id":"missing-1"}"""),
            "INVALID_FEATURE_PARAMETERS"
        },
        {
            Encoding.UTF8.GetBytes(
                """{"schema_version":"1.12","method":"status","request_id":"extra-1","params":{},"unexpected":true}"""),
            "INVALID_FEATURE_PARAMETERS"
        },
        {
            Encoding.UTF8.GetBytes(
                """{"schema_version":"9.9","method":"status","request_id":"schema-1","params":{}}"""),
            "UNSUPPORTED_SCHEMA_VERSION"
        },
        { Encoding.UTF8.GetBytes(new string('x', 129)), "INVALID_FEATURE_PARAMETERS" },
        {
            Encoding.UTF8.GetBytes(
                """{"schema_version":"1.12","method":"status","request_id":"depth-1","params":{"a":{"b":{"c":{"d":1}}}}}"""),
            "INVALID_FEATURE_PARAMETERS"
        },
    };

    [Theory]
    [MemberData(nameof(RejectedPayloads))]
    public async Task InvalidPayloadNeverInvokesHandler(byte[] payload, string expectedCode)
    {
        var processor = CreateProcessor(maxRequestBytes: 128, maxRequestDepth: 4);
        var handlerCalls = 0;

        var response = await processor.ProcessPayloadAsync(
            payload,
            (_, _) =>
            {
                Interlocked.Increment(ref handlerCalls);
                return ValueTask.FromResult(PipeHandlerResult.Ok());
            });

        Assert.Equal(0, handlerCalls);
        Assert.Equal("rejected", Text(response, "status"));
        Assert.Equal(expectedCode, ErrorText(response, "code"));
    }

    [Fact]
    public async Task HandlerExceptionAlwaysReturnsSanitizedEnvelope()
    {
        var processor = CreateProcessor();

        var response = await processor.ProcessPayloadAsync(
            ValidPayload("failure-1"),
            (_, _) => throw new InvalidOperationException(
                @"sensitive C:\secret\drawing.dwg stack-marker"));

        Assert.Equal("failed", Text(response, "status"));
        Assert.Equal("INTERNAL_ERROR", ErrorText(response, "code"));
        Assert.Equal("The bridge handler failed safely.", ErrorText(response, "message"));

        var wire = response.ToJsonString();
        Assert.DoesNotContain("secret", wire.ToLowerInvariant());
        Assert.DoesNotContain("stack-marker", wire);
        Assert.DoesNotContain(nameof(InvalidOperationException), wire);
    }

    [Fact]
    public async Task Property14_IpcBoundaryAlwaysReturnsEnvelopeAndRejectsInvalidBeforeHandler()
    {
        const int exampleCount = 168;
        const int maximumRequestBytes = 256;
        var random = new Random(0x14_2026);

        for (var example = 0; example < exampleCount; example++)
        {
            var caseKind = (Property14CaseKind)(example % 7);
            var requestId = $"property-14-{example}";
            var domainFailureStatus = random.Next(3);
            var payload = CreateProperty14Payload(
                caseKind,
                requestId,
                random,
                maximumRequestBytes);
            var processor = CreateProcessor(
                maxRequestBytes: maximumRequestBytes,
                maxRequestDepth: 6);
            var handlerCalls = 0;
            var transactionOpens = 0;

            ValueTask<PipeHandlerResult> Handler(JsonElement _, CancellationToken __)
            {
                handlerCalls++;
                transactionOpens++;
                if (caseKind == Property14CaseKind.HandlerThrows)
                {
                    throw new ExpectedProperty14Exception();
                }

                if (caseKind != Property14CaseKind.HandlerDomainFailure)
                {
                    throw new Xunit.Sdk.XunitException(
                        $"Invalid example {example} reached the handler.");
                }

                var error = new IpcError(
                    "PROPERTY_HANDLER_FAILURE",
                    "The generated handler rejected the request.");
                var result = domainFailureStatus switch
                {
                    0 => PipeHandlerResult.Rejected(error),
                    1 => PipeHandlerResult.Conflict(error),
                    _ => PipeHandlerResult.Failed(error),
                };
                return ValueTask.FromResult(result);
            }

            var response = await processor.ProcessPayloadAsync(payload, Handler);
            AssertTerminalResponseEnvelope(response);

            var validRequest = caseKind is
                Property14CaseKind.HandlerThrows or Property14CaseKind.HandlerDomainFailure;
            Assert.Equal(validRequest ? 1 : 0, handlerCalls);
            Assert.Equal(validRequest ? 1 : 0, transactionOpens);

            if (!validRequest)
            {
                Assert.Equal("rejected", Text(response, "status"));
                var expectedCode = caseKind == Property14CaseKind.UnsupportedSchema
                    ? "UNSUPPORTED_SCHEMA_VERSION"
                    : "INVALID_FEATURE_PARAMETERS";
                Assert.Equal(expectedCode, ErrorText(response, "code"));
                continue;
            }

            if (caseKind == Property14CaseKind.HandlerThrows)
            {
                Assert.Equal("failed", Text(response, "status"));
                Assert.Equal("INTERNAL_ERROR", ErrorText(response, "code"));
                continue;
            }

            var expectedStatus = domainFailureStatus switch
            {
                0 => "rejected",
                1 => "conflict",
                _ => "failed",
            };
            Assert.Equal(expectedStatus, Text(response, "status"));
            Assert.Equal("PROPERTY_HANDLER_FAILURE", ErrorText(response, "code"));
            Assert.Equal(requestId, Text(response, "request_id"));
        }
    }

    [Theory]
    [InlineData("rejected")]
    [InlineData("conflict")]
    [InlineData("failed")]
    public async Task DomainErrorResultPassesThroughTypedFieldsWithoutOwningEnvelope(string status)
    {
        var processor = CreateProcessor();
        using var detailDocument = JsonDocument.Parse("\"revision-42\"");
        var error = new CadBridge.Contracts.IpcError(
            "STALE_DOCUMENT_REVISION",
            "The drawing revision changed.")
        {
            Details = new Dictionary<string, JsonElement>
            {
                ["actual_revision"] = detailDocument.RootElement.Clone(),
            },
            RequiredAction = "Inspect the current revision and preview again.",
            Retryable = true,
        };
        var expected = status switch
        {
            "rejected" => PipeHandlerResult.Rejected(error),
            "conflict" => PipeHandlerResult.Conflict(error),
            "failed" => PipeHandlerResult.Failed(error),
            _ => throw new Xunit.Sdk.XunitException("Unexpected test status."),
        };

        var response = await processor.ProcessPayloadAsync(
            ValidPayload("domain-error-1"),
            (_, _) => ValueTask.FromResult(expected));

        Assert.Equal("1.12", Text(response, "schema_version"));
        Assert.Equal("domain-error-1", Text(response, "request_id"));
        Assert.Equal(status, Text(response, "status"));
        Assert.Equal("STALE_DOCUMENT_REVISION", ErrorText(response, "code"));
        Assert.Equal("The drawing revision changed.", ErrorText(response, "message"));
        Assert.True(response["error"]!["retryable"]!.GetValue<bool>());
        Assert.Equal(
            "Inspect the current revision and preview again.",
            ErrorText(response, "required_action"));
        Assert.Equal(
            "revision-42",
            response["error"]!["details"]!["actual_revision"]!.GetValue<string>());
    }

    [Fact]
    public async Task DuplicateActiveRequestIsRejectedWithoutSecondHandlerInvocation()
    {
        var processor = CreateProcessor();
        var entered = NewSignal();
        var release = NewSignal();
        var handlerCalls = 0;

        async ValueTask<PipeHandlerResult> Handler(JsonElement _, CancellationToken cancellationToken)
        {
            var call = Interlocked.Increment(ref handlerCalls);
            if (call == 1)
            {
                entered.TrySetResult();
                await release.Task.WaitAsync(cancellationToken);
            }

            return PipeHandlerResult.Ok();
        }

        var first = processor.ProcessPayloadAsync(ValidPayload("duplicate-1"), Handler).AsTask();
        await entered.Task;

        var duplicate = await processor.ProcessPayloadAsync(ValidPayload("duplicate-1"), Handler);

        Assert.Equal(1, handlerCalls);
        Assert.Equal("rejected", Text(duplicate, "status"));
        Assert.Equal("INVALID_FEATURE_PARAMETERS", ErrorText(duplicate, "code"));
        Assert.False(first.IsCompleted);

        release.TrySetResult();
        Assert.Equal("ok", Text(await first, "status"));
    }

    [Fact]
    public async Task CancelEnvelopeTargetsOneRequestWaitsForTerminationAndAllowsIdReuse()
    {
        var processor = CreateProcessor();
        var targetEntered = NewSignal();
        var neighborEntered = NewSignal();
        var targetCancellationObserved = NewSignal();
        var neighborCancellationObserved = NewSignal();
        var terminateTarget = NewSignal();
        var terminateNeighbor = NewSignal();
        var handlerCalls = 0;

        async ValueTask<PipeHandlerResult> Handler(JsonElement request, CancellationToken cancellationToken)
        {
            Interlocked.Increment(ref handlerCalls);
            var requestId = request.GetProperty("request_id").GetString();
            if (requestId == "target-1")
            {
                using var registration = cancellationToken.Register(
                    static state => ((TaskCompletionSource)state!).TrySetResult(),
                    targetCancellationObserved);
                targetEntered.TrySetResult();
                await terminateTarget.Task;
                cancellationToken.ThrowIfCancellationRequested();
                return PipeHandlerResult.Ok();
            }

            using var neighborRegistration = cancellationToken.Register(
                static state => ((TaskCompletionSource)state!).TrySetResult(),
                neighborCancellationObserved);
            neighborEntered.TrySetResult();
            await terminateNeighbor.Task;
            cancellationToken.ThrowIfCancellationRequested();
            return PipeHandlerResult.Ok();
        }

        var target = processor.ProcessPayloadAsync(ValidPayload("target-1"), Handler).AsTask();
        var neighbor = processor.ProcessPayloadAsync(ValidPayload("neighbor-1"), Handler).AsTask();
        await Task.WhenAll(targetEntered.Task, neighborEntered.Task);

        var cancel = processor.ProcessPayloadAsync(
            CancelPayload("cancel-1", "target-1"),
            Handler).AsTask();
        await targetCancellationObserved.Task;
        Assert.Equal(2, handlerCalls);
        Assert.False(target.IsCompleted);
        Assert.False(neighbor.IsCompleted);
        Assert.False(cancel.IsCompleted);
        Assert.False(neighborCancellationObserved.Task.IsCompleted);

        terminateTarget.TrySetResult();
        var cancelled = await target;
        Assert.Equal("failed", Text(cancelled, "status"));
        Assert.Equal("IPC_TIMEOUT", ErrorText(cancelled, "code"));
        var acknowledgement = await cancel;
        Assert.Equal("ok", Text(acknowledgement, "status"));
        Assert.Equal("target-1", acknowledgement["data"]!["cancelled_request_id"]!.GetValue<string>());
        Assert.True(acknowledgement["data"]!["terminal"]!.GetValue<bool>());
        Assert.Equal(2, handlerCalls);

        var reused = await processor.ProcessPayloadAsync(
            ValidPayload("target-1"),
            (_, _) => ValueTask.FromResult(PipeHandlerResult.Ok()));
        Assert.Equal("ok", Text(reused, "status"));

        terminateNeighbor.TrySetResult();
        Assert.Equal("ok", Text(await neighbor, "status"));
        Assert.False(neighborCancellationObserved.Task.IsCompleted);
    }

    [Fact]
    public async Task CancelBeforeRegistrationTombstonesTargetAndAcknowledgesOnlyAfterTerminal()
    {
        var processor = CreateProcessor();
        var handlerCalls = 0;

        var cancel = processor.ProcessPayloadAsync(
            CancelPayload("cancel-early-1", "late-target-1"),
            UnexpectedHandler).AsTask();

        Assert.False(cancel.IsCompleted);

        var target = processor.ProcessPayloadAsync(
            ValidPayload("late-target-1"),
            (_, _) =>
            {
                Interlocked.Increment(ref handlerCalls);
                return ValueTask.FromResult(PipeHandlerResult.Ok());
            }).AsTask();

        var terminal = await target;
        Assert.Equal(0, handlerCalls);
        Assert.Equal("failed", Text(terminal, "status"));
        Assert.Equal("IPC_TIMEOUT", ErrorText(terminal, "code"));

        var acknowledgement = await cancel;
        Assert.Equal("ok", Text(acknowledgement, "status"));
        Assert.Equal(
            "late-target-1",
            acknowledgement["data"]!["cancelled_request_id"]!.GetValue<string>());
        Assert.True(acknowledgement["data"]!["terminal"]!.GetValue<bool>());
    }

    [Fact]
    public async Task CancelControlTimeoutNeverClaimsNonCooperativeTargetIsTerminal()
    {
        var processor = CreateProcessor();
        var targetEntered = NewSignal();
        var cancellationObserved = NewSignal();
        var releaseTarget = NewSignal();

        async ValueTask<PipeHandlerResult> NonCooperativeHandler(
            JsonElement _,
            CancellationToken cancellationToken)
        {
            using var registration = cancellationToken.Register(
                static state => ((TaskCompletionSource)state!).TrySetResult(),
                cancellationObserved);
            targetEntered.TrySetResult();
            await releaseTarget.Task;
            return PipeHandlerResult.Ok();
        }

        var target = processor.ProcessPayloadAsync(
            ValidPayload("stubborn-target-1"),
            NonCooperativeHandler).AsTask();
        await targetEntered.Task;

        using var cancelDeadline = new CancellationTokenSource();
        var cancel = processor.ProcessPayloadAsync(
            CancelPayload("cancel-stubborn-1", "stubborn-target-1"),
            UnexpectedHandler,
            cancelDeadline.Token).AsTask();
        await cancellationObserved.Task;

        Assert.False(target.IsCompleted);
        Assert.False(cancel.IsCompleted);

        cancelDeadline.Cancel();
        var controlResponse = await cancel;
        Assert.Equal("failed", Text(controlResponse, "status"));
        Assert.Equal("IPC_TIMEOUT", ErrorText(controlResponse, "code"));
        Assert.Null(controlResponse["data"]);
        Assert.DoesNotContain("\"terminal\":true", controlResponse.ToJsonString());

        releaseTarget.TrySetResult();
        var terminal = await target;
        Assert.Equal("failed", Text(terminal, "status"));
        Assert.Equal("IPC_TIMEOUT", ErrorText(terminal, "code"));
    }

    [Fact]
    public async Task FramingUsesBigEndianAndRejectsTruncatedFrames()
    {
        var processor = CreateProcessor();
        var payload = ValidPayload("frame-1");
        var inputFrame = Frame(payload);
        Assert.Equal(payload.Length, BinaryPrimitives.ReadInt32BigEndian(inputFrame.AsSpan(0, 4)));

        var responseFrame = await processor.ProcessNextAsync(
            new MemoryStream(inputFrame),
            (_, _) => ValueTask.FromResult(PipeHandlerResult.Ok()));

        var responseLength = BinaryPrimitives.ReadInt32BigEndian(responseFrame.AsSpan(0, 4));
        Assert.Equal(responseFrame.Length - 4, responseLength);
        Assert.Equal("ok", Text(ParseFrame(responseFrame), "status"));

        var truncatedHeader = await processor.ProcessNextAsync(
            new MemoryStream([0, 0, 1]),
            UnexpectedHandler);
        Assert.Equal("INVALID_FEATURE_PARAMETERS", ErrorText(ParseFrame(truncatedHeader), "code"));

        var truncatedBody = new byte[6];
        BinaryPrimitives.WriteInt32BigEndian(truncatedBody.AsSpan(0, 4), 10);
        truncatedBody[4] = (byte)'{';
        truncatedBody[5] = (byte)'}';
        var truncatedBodyResponse = await processor.ProcessNextAsync(
            new MemoryStream(truncatedBody),
            UnexpectedHandler);
        Assert.Equal("INVALID_FEATURE_PARAMETERS", ErrorText(ParseFrame(truncatedBodyResponse), "code"));
    }

    private static PipeRequestProcessor CreateProcessor(
        int maxRequestBytes = 1_048_576,
        int maxRequestDepth = 32) =>
        new(
            new PipeServerOptions
            {
                PipeNameTemplate = "cad-harness-{user_sid}",
                UserSid = "S-1-5-21-1000",
                MaxRequestBytes = maxRequestBytes,
                MaxRequestDepth = maxRequestDepth,
            });

    private static byte[] CreateProperty14Payload(
        Property14CaseKind caseKind,
        string requestId,
        Random random,
        int maximumRequestBytes)
    {
        switch (caseKind)
        {
            case Property14CaseKind.MalformedUtf8:
                {
                    var payload = new byte[random.Next(1, maximumRequestBytes + 1)];
                    random.NextBytes(payload);
                    payload[0] = 0xff;
                    return payload;
                }

            case Property14CaseKind.Oversized:
                {
                    var payload = new byte[random.Next(maximumRequestBytes + 1, maximumRequestBytes + 129)];
                    random.NextBytes(payload);
                    return payload;
                }

            case Property14CaseKind.ExcessiveDepth:
                {
                    var nested = "0";
                    for (var depth = 0; depth < random.Next(8, 17); depth++)
                    {
                        nested = $"{{\"nested\":{nested}}}";
                    }

                    return Encoding.UTF8.GetBytes(
                        $"{{\"schema_version\":\"1.12\",\"method\":\"status\"," +
                        $"\"request_id\":\"{requestId}\",\"params\":{{\"value\":{nested}}}}}");
                }

            case Property14CaseKind.UnsupportedSchema:
                return Encoding.UTF8.GetBytes(
                    $"{{\"schema_version\":\"9.{random.Next(1, 10)}\",\"method\":\"status\"," +
                    $"\"request_id\":\"{requestId}\",\"params\":{{}}}}");

            case Property14CaseKind.InvalidEnvelope:
                return Encoding.UTF8.GetBytes(random.Next(6) switch
                {
                    0 => $"{{\"schema_version\":\"1.12\",\"method\":\"status\",\"request_id\":\"{requestId}\"}}",
                    1 => $"{{\"schema_version\":\"1.12\",\"method\":\"arbitrary\",\"request_id\":\"{requestId}\",\"params\":{{}}}}",
                    2 => "{\"schema_version\":\"1.12\",\"method\":\"status\",\"request_id\":\"\",\"params\":{}}",
                    3 => $"{{\"schema_version\":\"1.12\",\"method\":\"status\",\"request_id\":\"{requestId}\",\"params\":{{}},\"extra\":true}}",
                    4 => $"{{\"schema_version\":\"1.12\",\"method\":\"status\",\"request_id\":\"{requestId}\",\"params\":[]}}",
                    _ => $"{{\"schema_version\":\"1.12\",\"method\":\"status\",\"request_id\":\"{requestId}\",\"request_id\":\"duplicate\",\"params\":{{}}}}",
                });

            case Property14CaseKind.HandlerThrows:
            case Property14CaseKind.HandlerDomainFailure:
                return ValidPayload(requestId);

            default:
                throw new Xunit.Sdk.XunitException("Unknown generated IPC case.");
        }
    }

    private static void AssertTerminalResponseEnvelope(JsonObject response)
    {
        var typed = IpcJson.Deserialize<IpcResponse>(response.ToJsonString());
        Assert.Equal(IpcContract.CurrentSchemaVersion, typed.SchemaVersion);
        Assert.InRange(typed.RequestId.Length, 1, 64);

        if (typed.Status == IpcResponseStatus.Ok)
        {
            Assert.NotNull(typed.Data);
            Assert.Null(typed.Error);
            return;
        }

        Assert.Null(typed.Data);
        Assert.NotNull(typed.Error);
        Assert.False(string.IsNullOrWhiteSpace(typed.Error.Code));
        Assert.False(string.IsNullOrWhiteSpace(typed.Error.Message));
        Assert.NotNull(typed.Error.Details);
    }

    private static byte[] ValidPayload(string requestId) => Encoding.UTF8.GetBytes(
        $"{{\"schema_version\":\"1.12\",\"method\":\"status\",\"request_id\":\"{requestId}\",\"params\":{{}}}}");

    private static byte[] CancelPayload(string requestId, string targetRequestId) => Encoding.UTF8.GetBytes(
        $"{{\"schema_version\":\"1.12\",\"method\":\"cancel\",\"request_id\":\"{requestId}\"," +
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
        var declaredLength = BinaryPrimitives.ReadInt32BigEndian(frame.AsSpan(0, 4));
        Assert.Equal(frame.Length - 4, declaredLength);
        return JsonNode.Parse(frame.AsSpan(4))!.AsObject();
    }

    private static ValueTask<PipeHandlerResult> UnexpectedHandler(JsonElement _, CancellationToken __) =>
        throw new Xunit.Sdk.XunitException("Rejected framing must not invoke the handler.");

    private static TaskCompletionSource NewSignal() =>
        new(TaskCreationOptions.RunContinuationsAsynchronously);

    private static string Text(JsonObject response, string propertyName) =>
        response[propertyName]!.GetValue<string>();

    private static string ErrorText(JsonObject response, string propertyName) =>
        response["error"]![propertyName]!.GetValue<string>();

    private enum Property14CaseKind
    {
        MalformedUtf8,
        Oversized,
        ExcessiveDepth,
        UnsupportedSchema,
        InvalidEnvelope,
        HandlerThrows,
        HandlerDomainFailure,
    }

    private sealed class ExpectedProperty14Exception : Exception;
}
