using System.Buffers;
using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;
using CadBridge.Contracts;

namespace CadBridge.Hosting;

/// <summary>Validated authority carried by a versioned, self-contained approval token.</summary>
public sealed record BridgeApprovalClaims(
    string ApprovalId,
    string JobId,
    string DocumentId,
    string ExpectedRevision,
    string PlanHash,
    string ApprovedBy,
    DateTimeOffset ExpiresAt);

/// <summary>
/// Validated authority carried by a rollback-only approval token.  This is deliberately
/// independent from <see cref="BridgeApprovalClaims"/>: a commit approval must never
/// authorize destructive checkpoint restore.
/// </summary>
public sealed record BridgeRollbackApprovalClaims(
    string SchemaVersion,
    string ApprovalId,
    string JobId,
    string DocumentId,
    string CheckpointId,
    string CurrentRevision,
    string ApprovedBy,
    DateTimeOffset ApprovedAt,
    DateTimeOffset ExpiresAt,
    string ApprovalTokenDigest);

/// <summary>
/// Cross-language authorization primitives shared by the strict router host. The canonical plan
/// hash mirrors cad_harness.domain.canonical: sorted UTF-8 JSON, nine decimal places, and recursive
/// removal of volatile/instance identity fields.
/// </summary>
public static class BridgeAuthorization
{
    private const int FloatPrecision = 9;
    private const int MaximumTokenLength = 4096;

    private static readonly HashSet<string> ExcludedPlanFields =
    [
        "plan_hash",
        "created_at",
        "updated_at",
        "request_id",
        "trace_id",
        "audit_event_id",
        "plan_id",
        "job_id",
    ];

    private static readonly HashSet<string> ClaimFields =
    [
        "approval_id",
        "job_id",
        "document_id",
        "expected_revision",
        "plan_hash",
        "approved_by",
        "expires_at",
    ];

    private static readonly HashSet<string> RollbackClaimFields =
    [
        "schema_version",
        "approval_id",
        "job_id",
        "document_id",
        "checkpoint_id",
        "current_revision",
        "approved_by",
        "approved_at",
        "expires_at",
    ];

    public static string ComputePlanHash(JsonElement plan)
    {
        var buffer = new ArrayBufferWriter<byte>();
        using (var writer = new Utf8JsonWriter(
            buffer,
            new JsonWriterOptions
            {
                Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
                Indented = false,
            }))
        {
            WriteCanonical(writer, plan);
        }

        return $"sha256:{Convert.ToHexString(SHA256.HashData(buffer.WrittenSpan)).ToLowerInvariant()}";
    }

    public static bool TryValidateCommitAuthorization(
        JsonElement plan,
        string approvalToken,
        string secret,
        string jobId,
        string expectedRevision,
        DateTimeOffset now,
        out BridgeApprovalClaims? claims)
    {
        claims = null;
        if (string.IsNullOrEmpty(secret) ||
            string.IsNullOrEmpty(approvalToken) ||
            approvalToken.Length > MaximumTokenLength ||
            plan.ValueKind != JsonValueKind.Object ||
            !TryRequiredString(plan, "schema_version", out var planSchemaVersion) ||
            !FixedEquals(planSchemaVersion, IpcContract.CurrentSchemaVersion))
        {
            return false;
        }

        var parts = approvalToken.Split('.', StringSplitOptions.None);
        if (parts.Length != 3 || parts[0] != "v2" || parts[1].Length == 0 ||
            parts[2].Length != 64 || parts[2].Any(character => !Uri.IsHexDigit(character)))
        {
            return false;
        }

        byte[] suppliedSignature;
        try
        {
            suppliedSignature = Convert.FromHexString(parts[2]);
        }
        catch (FormatException)
        {
            return false;
        }

        var expectedSignature = HMACSHA256.HashData(
            Encoding.UTF8.GetBytes(secret),
            Encoding.ASCII.GetBytes(parts[1]));
        if (!CryptographicOperations.FixedTimeEquals(suppliedSignature, expectedSignature) ||
            !TryDecodeBase64Url(parts[1], out var payload) ||
            payload.Length is 0 or > 16_384)
        {
            return false;
        }

        try
        {
            using var document = JsonDocument.Parse(
                payload,
                new JsonDocumentOptions
                {
                    AllowTrailingCommas = false,
                    CommentHandling = JsonCommentHandling.Disallow,
                    MaxDepth = 8,
                });
            var root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object ||
                !root.EnumerateObject().Select(property => property.Name)
                    .ToHashSet(StringComparer.Ordinal).SetEquals(ClaimFields) ||
                root.EnumerateObject().Count() != ClaimFields.Count ||
                !TryRequiredString(root, "approval_id", out var approvalId) ||
                !TryRequiredString(root, "job_id", out var claimJobId) ||
                !TryRequiredString(root, "document_id", out var documentId) ||
                !TryRequiredString(root, "expected_revision", out var claimRevision) ||
                !TryRequiredString(root, "plan_hash", out var claimPlanHash) ||
                !TryRequiredString(root, "approved_by", out var approvedBy) ||
                !TryRequiredString(root, "expires_at", out var expiresAtText) ||
                !DateTimeOffset.TryParse(
                    expiresAtText,
                    CultureInfo.InvariantCulture,
                    DateTimeStyles.AllowWhiteSpaces | DateTimeStyles.AssumeUniversal,
                    out var expiresAt) ||
                !plan.TryGetProperty("document_id", out var planDocument) ||
                planDocument.ValueKind != JsonValueKind.String ||
                !plan.TryGetProperty("plan_hash", out var suppliedPlanHash) ||
                suppliedPlanHash.ValueKind != JsonValueKind.String)
            {
                return false;
            }

            var computedPlanHash = ComputePlanHash(plan);
            if (!FixedEquals(claimJobId, jobId) ||
                !FixedEquals(documentId, planDocument.GetString()!) ||
                !FixedEquals(claimRevision, expectedRevision) ||
                !FixedEquals(claimPlanHash, suppliedPlanHash.GetString()!) ||
                !FixedEquals(claimPlanHash, computedPlanHash) ||
                now > expiresAt)
            {
                return false;
            }

            claims = new BridgeApprovalClaims(
                approvalId,
                claimJobId,
                documentId,
                claimRevision,
                claimPlanHash,
                approvedBy,
                expiresAt);
            return true;
        }
        catch (JsonException)
        {
            return false;
        }
        catch (InvalidOperationException)
        {
            return false;
        }
    }

    /// <summary>
    /// Verifies the Python <c>rb1</c> token contract. The MAC is checked before payload
    /// decoding so untrusted/tampered claims cannot influence authorization decisions.
    /// </summary>
    public static bool TryValidateRollbackAuthorization(
        string rollbackApprovalToken,
        string secret,
        string jobId,
        string documentId,
        string checkpointId,
        string currentRevision,
        DateTimeOffset now,
        out BridgeRollbackApprovalClaims? claims)
    {
        return TryValidateRollbackAuthorizationCore(
            rollbackApprovalToken,
            secret,
            jobId,
            documentId,
            checkpointId,
            currentRevision,
            now,
            enforceExpiry: true,
            out claims);
    }

    /// <summary>
    /// Validates an expired rollback token only when an authenticated durable journal proves that
    /// the exact approval and scope already crossed the Prepared boundary. This path can resume or
    /// replay that recorded attempt; it cannot authorize a new replacement.
    /// </summary>
    public static bool TryValidateRollbackRecoveryAuthorization(
        string rollbackApprovalToken,
        string secret,
        string jobId,
        string documentId,
        string checkpointId,
        string currentRevision,
        DateTimeOffset now,
        Func<BridgeRollbackApprovalClaims, bool> canResumeExactRecordedAttempt,
        out BridgeRollbackApprovalClaims? claims)
    {
        ArgumentNullException.ThrowIfNull(canResumeExactRecordedAttempt);
        claims = null;
        if (!TryValidateRollbackAuthorizationCore(
                rollbackApprovalToken,
                secret,
                jobId,
                documentId,
                checkpointId,
                currentRevision,
                now,
                enforceExpiry: false,
                out var candidate) ||
            candidate is null ||
            now <= candidate.ExpiresAt ||
            !canResumeExactRecordedAttempt(candidate))
        {
            return false;
        }

        claims = candidate;
        return true;
    }

    private static bool TryValidateRollbackAuthorizationCore(
        string rollbackApprovalToken,
        string secret,
        string jobId,
        string documentId,
        string checkpointId,
        string currentRevision,
        DateTimeOffset now,
        bool enforceExpiry,
        out BridgeRollbackApprovalClaims? claims)
    {
        claims = null;
        if (string.IsNullOrEmpty(secret) ||
            string.IsNullOrEmpty(rollbackApprovalToken) ||
            rollbackApprovalToken.Length > MaximumTokenLength)
        {
            return false;
        }

        var parts = rollbackApprovalToken.Split('.', StringSplitOptions.None);
        if (parts.Length != 3 || parts[0] != "rb1" || parts[1].Length == 0 ||
            parts[2].Length != 64 || parts[2].Any(character => !Uri.IsHexDigit(character)))
        {
            return false;
        }

        byte[] suppliedSignature;
        try
        {
            suppliedSignature = Convert.FromHexString(parts[2]);
        }
        catch (FormatException)
        {
            return false;
        }

        var expectedSignature = HMACSHA256.HashData(
            Encoding.UTF8.GetBytes(secret),
            Encoding.ASCII.GetBytes(parts[1]));
        if (!CryptographicOperations.FixedTimeEquals(suppliedSignature, expectedSignature) ||
            !TryDecodeBase64Url(parts[1], out var payload) ||
            payload.Length is 0 or > 16_384)
        {
            return false;
        }

        try
        {
            using var document = JsonDocument.Parse(
                payload,
                new JsonDocumentOptions
                {
                    AllowTrailingCommas = false,
                    CommentHandling = JsonCommentHandling.Disallow,
                    MaxDepth = 8,
                });
            var root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object ||
                !root.EnumerateObject().Select(property => property.Name)
                    .ToHashSet(StringComparer.Ordinal).SetEquals(RollbackClaimFields) ||
                root.EnumerateObject().Count() != RollbackClaimFields.Count ||
                !TryRequiredString(root, "schema_version", out var schemaVersion) ||
                !TryRequiredString(root, "approval_id", out var approvalId) ||
                !TryRequiredString(root, "job_id", out var claimJobId) ||
                !TryRequiredString(root, "document_id", out var claimDocumentId) ||
                !TryRequiredString(root, "checkpoint_id", out var claimCheckpointId) ||
                !TryRequiredString(root, "current_revision", out var claimRevision) ||
                !TryRequiredString(root, "approved_by", out var approvedBy) ||
                !TryRequiredString(root, "approved_at", out var approvedAtText) ||
                !TryRequiredString(root, "expires_at", out var expiresAtText) ||
                !FixedEquals(schemaVersion, IpcContract.CurrentSchemaVersion) ||
                !DateTimeOffset.TryParse(
                    approvedAtText,
                    CultureInfo.InvariantCulture,
                    DateTimeStyles.AllowWhiteSpaces | DateTimeStyles.AssumeUniversal,
                    out var approvedAt) ||
                !DateTimeOffset.TryParse(
                    expiresAtText,
                    CultureInfo.InvariantCulture,
                    DateTimeStyles.AllowWhiteSpaces | DateTimeStyles.AssumeUniversal,
                    out var expiresAt) ||
                !FixedEquals(claimJobId, jobId) ||
                !FixedEquals(claimDocumentId, documentId) ||
                !FixedEquals(claimCheckpointId, checkpointId) ||
                !FixedEquals(claimRevision, currentRevision) ||
                (enforceExpiry && now > expiresAt))
            {
                return false;
            }

            claims = new BridgeRollbackApprovalClaims(
                schemaVersion,
                approvalId,
                claimJobId,
                claimDocumentId,
                claimCheckpointId,
                claimRevision,
                approvedBy,
                approvedAt,
                expiresAt,
                ComputeRollbackApprovalTokenDigest(rollbackApprovalToken));
            return true;
        }
        catch (JsonException)
        {
            return false;
        }
        catch (InvalidOperationException)
        {
            return false;
        }
    }

    private static void WriteCanonical(Utf8JsonWriter writer, JsonElement value)
    {
        switch (value.ValueKind)
        {
            case JsonValueKind.Object:
                writer.WriteStartObject();
                foreach (var property in value.EnumerateObject()
                    .Where(property => !ExcludedPlanFields.Contains(property.Name))
                    .OrderBy(property => property.Name, StringComparer.Ordinal))
                {
                    writer.WritePropertyName(property.Name);
                    WriteCanonical(writer, property.Value);
                }

                writer.WriteEndObject();
                break;
            case JsonValueKind.Array:
                writer.WriteStartArray();
                foreach (var item in value.EnumerateArray())
                {
                    WriteCanonical(writer, item);
                }

                writer.WriteEndArray();
                break;
            case JsonValueKind.String:
                writer.WriteStringValue(value.GetString());
                break;
            case JsonValueKind.Number:
                writer.WriteRawValue(CanonicalNumber(value), skipInputValidation: true);
                break;
            case JsonValueKind.True:
                writer.WriteBooleanValue(true);
                break;
            case JsonValueKind.False:
                writer.WriteBooleanValue(false);
                break;
            case JsonValueKind.Null:
                writer.WriteNullValue();
                break;
            default:
                throw new InvalidOperationException("The plan contains a non-JSON value.");
        }
    }

    private static string CanonicalNumber(JsonElement value)
    {
        if (value.TryGetInt64(out var integer))
        {
            return integer.ToString(CultureInfo.InvariantCulture);
        }

        if (!value.TryGetDouble(out var number) || !double.IsFinite(number))
        {
            throw new InvalidOperationException("The plan contains a non-finite or unsupported number.");
        }

        var rounded = Math.Round(number, FloatPrecision, MidpointRounding.ToEven);
        if (rounded == 0.0)
        {
            return "0.0";
        }

        var text = rounded.ToString("R", CultureInfo.InvariantCulture).Replace('E', 'e');
        var exponentAt = text.IndexOf('e');
        if (exponentAt < 0)
        {
            return text.Contains('.') ? text : $"{text}.0";
        }

        var mantissa = text[..exponentAt];
        var exponent = int.Parse(text[(exponentAt + 1)..], CultureInfo.InvariantCulture);
        return $"{mantissa}e{(exponent >= 0 ? "+" : "-")}{Math.Abs(exponent):00}";
    }

    private static bool TryRequiredString(JsonElement owner, string name, out string value)
    {
        value = string.Empty;
        return owner.TryGetProperty(name, out var property) &&
            property.ValueKind == JsonValueKind.String &&
            (value = property.GetString() ?? string.Empty).Length is > 0 and <= 512 &&
            !value.Any(char.IsControl);
    }

    private static bool TryDecodeBase64Url(string value, out byte[] payload)
    {
        payload = [];
        if (value.Any(character =>
                !(char.IsAsciiLetterOrDigit(character) || character is '-' or '_')))
        {
            return false;
        }

        var normalized = value.Replace('-', '+').Replace('_', '/');
        normalized += new string('=', (4 - (normalized.Length % 4)) % 4);
        try
        {
            payload = Convert.FromBase64String(normalized);
            return true;
        }
        catch (FormatException)
        {
            return false;
        }
    }

    private static bool FixedEquals(string left, string right) =>
        CryptographicOperations.FixedTimeEquals(
            Encoding.UTF8.GetBytes(left),
            Encoding.UTF8.GetBytes(right));

    private static string ComputeRollbackApprovalTokenDigest(string token) =>
        Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(
            "cad-harness-rb1-token-v1\0" + token))).ToLowerInvariant();
}
