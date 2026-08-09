using System.Security.Cryptography;
using System.Text;
using System.Globalization;
using System.Text.Json;

namespace CadBridge.Hosting;

/// <summary>Computes the exact stable identity of one authorized commit operation.</summary>
public static class CommitRequestDigest
{
    public static string Compute(CommitHostRequest request)
    {
        ArgumentNullException.ThrowIfNull(request);
        // The approval token is deliberately excluded: a refreshed token must not change the
        // identity of an otherwise identical commit. Job and idempotency key are both bound.
        var canonical = string.Join(
            "\u001f",
            "cad-bridge-commit-v1",
            request.JobId,
            request.IdempotencyKey,
            request.ExpectedRevision,
            request.CreateCheckpoint ? "1" : "0",
            CanonicalJson.Serialize(request.Plan));
        return Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(canonical)))
            .ToLowerInvariant();
    }
}

internal static class CanonicalJson
{
    public static string Serialize(JsonElement value)
    {
        using var stream = new MemoryStream();
        using (var writer = new Utf8JsonWriter(stream, new JsonWriterOptions
        {
            Indented = false,
            SkipValidation = false,
        }))
        {
            Write(value, writer);
        }

        return Encoding.UTF8.GetString(stream.ToArray());
    }

    private static void Write(JsonElement value, Utf8JsonWriter writer)
    {
        switch (value.ValueKind)
        {
            case JsonValueKind.Object:
                writer.WriteStartObject();
                var properties = value.EnumerateObject()
                    .OrderBy(property => property.Name, StringComparer.Ordinal)
                    .ToArray();
                for (var index = 1; index < properties.Length; index++)
                {
                    if (string.Equals(
                        properties[index - 1].Name,
                        properties[index].Name,
                        StringComparison.Ordinal))
                    {
                        throw new InvalidDataException(
                            "Canonical JSON does not permit duplicate object properties.");
                    }
                }

                foreach (var property in properties)
                {
                    writer.WritePropertyName(property.Name);
                    Write(property.Value, writer);
                }

                writer.WriteEndObject();
                break;
            case JsonValueKind.Array:
                writer.WriteStartArray();
                foreach (var item in value.EnumerateArray())
                {
                    Write(item, writer);
                }

                writer.WriteEndArray();
                break;
            case JsonValueKind.String:
                writer.WriteStringValue(value.GetString());
                break;
            case JsonValueKind.Number:
                writer.WriteRawValue(NormalizeNumber(value.GetRawText()));
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
                throw new InvalidDataException("The plan contains unsupported JSON data.");
        }
    }

    private static string NormalizeNumber(string raw)
    {
        if (decimal.TryParse(raw, NumberStyles.Float, CultureInfo.InvariantCulture, out var decimalValue))
        {
            return decimalValue == decimal.Zero
                ? "0"
                : decimalValue.ToString("G29", CultureInfo.InvariantCulture);
        }

        if (double.TryParse(raw, NumberStyles.Float, CultureInfo.InvariantCulture, out var doubleValue) &&
            double.IsFinite(doubleValue))
        {
            return doubleValue == 0.0
                ? "0"
                : doubleValue.ToString("R", CultureInfo.InvariantCulture);
        }

        throw new InvalidDataException("The plan contains a non-canonicalizable number.");
    }
}

/// <summary>
/// Applies durable idempotency around one host commit callback. The callback is never invoked for a
/// replay, a conflicting digest, an unresolved prior attempt, or a journal reservation failure.
/// </summary>
public sealed class DurableCommitCoordinator
{
    private readonly DurableCommitJournal _journal;

    public DurableCommitCoordinator(DurableCommitJournal journal)
    {
        ArgumentNullException.ThrowIfNull(journal);
        _journal = journal;
    }

    public async ValueTask<BridgeHostResult> ExecuteAsync(
        string jobId,
        string idempotencyKey,
        string digest,
        Func<bool> authorize,
        Func<CancellationToken, ValueTask<BridgeHostResult>> commit,
        CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(authorize);
        ArgumentNullException.ThrowIfNull(commit);
        try
        {
            if (!authorize())
            {
                return new BridgeHostResult(BridgeHostOutcome.Rejected);
            }
        }
        catch
        {
            return new BridgeHostResult(BridgeHostOutcome.Rejected);
        }

        CommitJournalDecision decision;
        try
        {
            decision = _journal.Begin(jobId, idempotencyKey, digest);
        }
        catch
        {
            // Reservation is durable-before-execute: a write must not begin if it cannot be stored.
            return new BridgeHostResult(BridgeHostOutcome.Failed);
        }

        switch (decision.Kind)
        {
            case CommitJournalDecisionKind.ReplayCommitted:
                return decision.Result ?? new BridgeHostResult(BridgeHostOutcome.Failed);
            case CommitJournalDecisionKind.Unknown:
                return new BridgeHostResult(BridgeHostOutcome.UnknownCommitState);
            case CommitJournalDecisionKind.IdempotencyKeyReused:
                return new BridgeHostResult(BridgeHostOutcome.IdempotencyKeyReused);
            case CommitJournalDecisionKind.Execute:
                break;
            default:
                return new BridgeHostResult(BridgeHostOutcome.Failed);
        }

        if (decision.ReservationId is not { } reservationId)
        {
            return new BridgeHostResult(BridgeHostOutcome.Failed);
        }

        BridgeHostResult result;
        try
        {
            result = await commit(cancellationToken);
        }
        catch
        {
            TryMarkUnknown(jobId, idempotencyKey, digest, reservationId);
            return new BridgeHostResult(BridgeHostOutcome.UnknownCommitState);
        }

        if (result.Outcome == BridgeHostOutcome.Ok)
        {
            try
            {
                _journal.MarkCommitted(jobId, idempotencyKey, digest, reservationId, result);
            }
            catch
            {
                TryMarkUnknown(jobId, idempotencyKey, digest, reservationId);
                return new BridgeHostResult(BridgeHostOutcome.UnknownCommitState);
            }
        }
        else if (result.Outcome == BridgeHostOutcome.UnknownCommitState)
        {
            TryMarkUnknown(jobId, idempotencyKey, digest, reservationId);
        }
        else
        {
            TryAbandon(jobId, idempotencyKey, digest, reservationId);
        }

        return result;
    }

    private void TryMarkUnknown(
        string jobId,
        string idempotencyKey,
        string digest,
        string reservationId)
    {
        try
        {
            _journal.MarkUnknown(jobId, idempotencyKey, digest, reservationId);
        }
        catch
        {
            // The flushed prepared entry remains fail-closed and recovers as unknown.
        }
    }

    private void TryAbandon(
        string jobId,
        string idempotencyKey,
        string digest,
        string reservationId)
    {
        try
        {
            _journal.Abandon(jobId, idempotencyKey, digest, reservationId);
        }
        catch
        {
            // Retaining prepared is conservative and prevents an unsafe retry.
        }
    }
}
