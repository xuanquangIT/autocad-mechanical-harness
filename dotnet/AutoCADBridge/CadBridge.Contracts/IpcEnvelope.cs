using System.ComponentModel.DataAnnotations;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace CadBridge.Contracts;

/// <summary>Wire contract version shared with the Python domain models.</summary>
public static class IpcContract
{
    public const string CurrentSchemaVersion = "1.13";
}

/// <summary>Methods accepted by the local AutoCAD bridge.</summary>
public enum IpcMethod
{
    Handshake,
    Status,
    InspectDocument,
    InspectSelection,
    Preview,
    ValidateRevision,
    Cancel,
    Commit,
    Rollback,
    Export,
}

/// <summary>Terminal response states defined by the IPC envelope contract.</summary>
public enum IpcResponseStatus
{
    Ok,
    Rejected,
    Conflict,
    Failed,
}

/// <summary>A request sent to the bridge over the local named pipe.</summary>
public sealed record IpcRequest(
    [property: JsonPropertyName("schema_version"), JsonRequired, RegularExpression(@"^\d+\.\d+$")]
    string SchemaVersion,
    [property: JsonPropertyName("method"), JsonRequired]
    IpcMethod Method,
    [property: JsonPropertyName("request_id"), JsonRequired, StringLength(64, MinimumLength = 1)]
    string RequestId,
    [property: JsonPropertyName("params"), JsonRequired]
    IReadOnlyDictionary<string, JsonElement> Params)
{
    [JsonPropertyName("idempotency_key")]
    [StringLength(128)]
    public string? IdempotencyKey { get; init; }

    [JsonPropertyName("job_id")]
    [StringLength(64)]
    public string? JobId { get; init; }
}

/// <summary>A response returned by the bridge.</summary>
public sealed record IpcResponse(
    [property: JsonPropertyName("schema_version"), JsonRequired, RegularExpression(@"^\d+\.\d+$")]
    string SchemaVersion,
    [property: JsonPropertyName("request_id"), JsonRequired, StringLength(64, MinimumLength = 1)]
    string RequestId,
    [property: JsonPropertyName("status"), JsonRequired]
    IpcResponseStatus Status)
{
    [JsonPropertyName("capabilities")]
    public IReadOnlyList<string>? Capabilities { get; init; }

    [JsonPropertyName("data")]
    public IReadOnlyDictionary<string, JsonElement>? Data { get; init; }

    [JsonPropertyName("error")]
    public IpcError? Error { get; init; }
}

/// <summary>A process-safe error payload without stack traces or host paths.</summary>
public sealed record IpcError(
    [property: JsonPropertyName("code"), JsonRequired]
    string Code,
    [property: JsonPropertyName("message"), JsonRequired]
    string Message)
{
    [JsonPropertyName("details")]
    public IReadOnlyDictionary<string, JsonElement>? Details { get; init; }

    [JsonPropertyName("required_action")]
    public string? RequiredAction { get; init; }

    [JsonPropertyName("retryable")]
    public bool Retryable { get; init; }
}

/// <summary>Strict, shared System.Text.Json settings for typed IPC envelopes.</summary>
public static class IpcJson
{
    public static JsonSerializerOptions Options { get; } = CreateOptions();

    public static string Serialize<TEnvelope>(TEnvelope envelope) =>
        JsonSerializer.Serialize(envelope, Options);

    public static TEnvelope Deserialize<TEnvelope>(string json) where TEnvelope : notnull =>
        JsonSerializer.Deserialize<TEnvelope>(json, Options)
        ?? throw new JsonException("The IPC envelope cannot be null.");

    private static JsonSerializerOptions CreateOptions()
    {
        var options = new JsonSerializerOptions
        {
            AllowTrailingCommas = false,
            DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
            NumberHandling = JsonNumberHandling.Strict,
            PropertyNameCaseInsensitive = false,
            ReadCommentHandling = JsonCommentHandling.Disallow,
            UnmappedMemberHandling = JsonUnmappedMemberHandling.Disallow,
        };
        options.Converters.Add(new JsonStringEnumConverter(JsonNamingPolicy.SnakeCaseLower, allowIntegerValues: false));
        return options;
    }
}
