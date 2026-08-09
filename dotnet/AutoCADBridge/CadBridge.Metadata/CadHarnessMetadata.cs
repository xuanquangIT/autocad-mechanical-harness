using CadBridge.Execution;

namespace CadBridge.Metadata;

/// <summary>Fixed application registry used exclusively by the bridge metadata writer.</summary>
public static class CadHarnessMetadataRegistry
{
    public const string ApplicationName = "CADHARNESS";
}

/// <summary>
/// Immutable, typed metadata attached to one entity. Identifiers are opaque correlation values: this
/// layer validates their bounded wire representation but never parses meaning from their contents.
/// </summary>
public sealed record CadHarnessMetadata
{
    public const int MaximumIdentifierLength = 128;

    private CadHarnessMetadata(string featureId, string operationId)
    {
        FeatureId = featureId;
        OperationId = operationId;
    }

    public string FeatureId { get; }

    public string OperationId { get; }

    public static CadHarnessMetadata Create(string featureId, string operationId) =>
        new(ValidateIdentifier(featureId, nameof(featureId)), ValidateIdentifier(operationId, nameof(operationId)));

    private static string ValidateIdentifier(string value, string parameterName)
    {
        ArgumentNullException.ThrowIfNull(value, parameterName);
        if (value.Length is < 1 or > MaximumIdentifierLength)
        {
            throw new ArgumentOutOfRangeException(
                parameterName,
                $"Identifier length must be between 1 and {MaximumIdentifierLength} ASCII characters.");
        }

        if (!IsAlphaNumeric(value[0]) || !IsAlphaNumeric(value[^1]))
        {
            throw new ArgumentException(
                "Identifier must begin and end with an ASCII letter or digit.",
                parameterName);
        }

        foreach (var character in value)
        {
            if (!IsAlphaNumeric(character) && character is not '-' and not '_' and not '.' and not ':')
            {
                throw new ArgumentException(
                    "Identifier may contain only ASCII letters, digits, hyphen, underscore, dot, and colon.",
                    parameterName);
            }
        }

        return value;
    }

    private static bool IsAlphaNumeric(char value) =>
        value is >= 'A' and <= 'Z' or >= 'a' and <= 'z' or >= '0' and <= '9';
}

/// <summary>
/// Opaque host entity reference. Autodesk-dependent code supplies an implementation backed by an
/// ObjectId; accepting this marker instead of a string prevents metadata APIs from becoming a raw
/// XData command surface.
/// </summary>
public interface IMetadataEntityReference
{
}

/// <summary>
/// Narrow capability exposed by the caller's active host transaction. The concrete host transaction
/// implements both this interface and <see cref="IAtomicTransaction"/>, but metadata writers receive
/// only this view, which deliberately has no begin, commit, or abort operation.
/// </summary>
public interface IActiveMetadataTransactionAccess
{
    bool IsActive { get; }
}

/// <summary>
/// Typed metadata access scoped to a transaction owned by the caller. Implementations must use the
/// supplied active transaction and must never start, commit, or abort a transaction themselves. The
/// fixed registry and field types are implementation constants; no raw registry name, XData type, or
/// arbitrary value can be injected through this contract.
/// </summary>
public interface IMetadataWriter
{
    ValueTask AttachAsync(
        IActiveMetadataTransactionAccess activeTransaction,
        IMetadataEntityReference newlyCreatedEntity,
        CadHarnessMetadata metadata,
        CancellationToken cancellationToken);

    ValueTask<CadHarnessMetadata?> ReadAsync(
        IActiveMetadataTransactionAccess activeTransaction,
        IMetadataEntityReference entity,
        CancellationToken cancellationToken);
}

/// <summary>Attaches typed metadata at the entity-creation boundary of the current transaction.</summary>
public sealed class MetadataAttachmentService
{
    private readonly IMetadataWriter _writer;

    public MetadataAttachmentService(IMetadataWriter writer)
    {
        ArgumentNullException.ThrowIfNull(writer);
        _writer = writer;
    }

    /// <summary>
    /// Call immediately after the entity is added to the database and before dispatch advances to the
    /// next operation. Cancellation is observed before any metadata write is requested.
    /// </summary>
    public ValueTask AttachImmediatelyAfterCreationAsync<TTransaction>(
        TTransaction activeTransaction,
        IMetadataEntityReference newlyCreatedEntity,
        string featureId,
        string operationId,
        CancellationToken cancellationToken)
        where TTransaction : IAtomicTransaction, IActiveMetadataTransactionAccess
    {
        ArgumentNullException.ThrowIfNull(activeTransaction);
        ArgumentNullException.ThrowIfNull(newlyCreatedEntity);
        EnsureActive(activeTransaction);
        var metadata = CadHarnessMetadata.Create(featureId, operationId);
        cancellationToken.ThrowIfCancellationRequested();
        return _writer.AttachAsync(activeTransaction, newlyCreatedEntity, metadata, cancellationToken);
    }

    /// <summary>Reads only the two typed harness fields through the caller's active transaction.</summary>
    public ValueTask<CadHarnessMetadata?> ReadAsync<TTransaction>(
        TTransaction activeTransaction,
        IMetadataEntityReference entity,
        CancellationToken cancellationToken)
        where TTransaction : IAtomicTransaction, IActiveMetadataTransactionAccess
    {
        ArgumentNullException.ThrowIfNull(activeTransaction);
        ArgumentNullException.ThrowIfNull(entity);
        EnsureActive(activeTransaction);
        cancellationToken.ThrowIfCancellationRequested();
        return _writer.ReadAsync(activeTransaction, entity, cancellationToken);
    }

    private static void EnsureActive(IActiveMetadataTransactionAccess transaction)
    {
        if (!transaction.IsActive)
        {
            throw new InvalidOperationException("Metadata access requires the caller's active transaction.");
        }
    }
}
