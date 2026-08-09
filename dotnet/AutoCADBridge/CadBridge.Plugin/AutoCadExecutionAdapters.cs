using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using CadBridge.Execution;
using CadBridge.Metadata;

namespace CadBridge.Plugin;

/// <summary>
/// Marshals the complete callback lifetime through AutoCAD's document command context. Cancellation
/// is observed before dispatch and again inside the callback; this adapter never returns while a
/// dispatched callback could still execute later.
/// </summary>
public sealed class AutoCadCommandContextMarshaller : ICommandContextMarshaller
{
    private readonly DocumentCollection _documents;

    public AutoCadCommandContextMarshaller(DocumentCollection documents)
    {
        ArgumentNullException.ThrowIfNull(documents);
        _documents = documents;
    }

    public async ValueTask<TResult> ExecuteAsync<TResult>(
        Func<CancellationToken, ValueTask<TResult>> callback,
        CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(callback);
        cancellationToken.ThrowIfCancellationRequested();
        var result = new CommandContextResult<TResult>();
        await _documents.ExecuteInCommandContextAsync(
            async _ =>
            {
                cancellationToken.ThrowIfCancellationRequested();
                result.Value = await callback(cancellationToken).ConfigureAwait(true);
                result.HasValue = true;
            },
            null);

        if (!result.HasValue)
        {
            throw new InvalidOperationException("AutoCAD did not execute the command-context callback.");
        }

        return result.Value!;
    }

    private sealed class CommandContextResult<TResult>
    {
        public bool HasValue { get; set; }

        public TResult? Value { get; set; }
    }

}

/// <summary>Atomic execution host permanently bound to one AutoCAD document.</summary>
public sealed class AutoCadAtomicDocumentHost : IAtomicDocumentHost
{
    private readonly Document _document;

    public AutoCadAtomicDocumentHost(Document document)
    {
        ArgumentNullException.ThrowIfNull(document);
        _document = document;
    }

    public IDocumentLock AcquireDocumentLock() =>
        new AutoCadDocumentLock(_document.LockDocument());

    public IUndoGroup BeginUndoGroup() => new AutoCadCommandContextUndoGroup(_document);

    public IAtomicTransaction BeginTransaction() =>
        new AutoCadAtomicTransaction(
            _document.Database,
            _document.Database.TransactionManager.StartTransaction());
}

/// <summary>Owns exactly one explicit AutoCAD document lock.</summary>
public sealed class AutoCadDocumentLock : IDocumentLock
{
    private DocumentLock? _documentLock;

    public AutoCadDocumentLock(DocumentLock documentLock)
    {
        ArgumentNullException.ThrowIfNull(documentLock);
        _documentLock = documentLock;
    }

    public void Dispose()
    {
        var heldLock = Interlocked.Exchange(ref _documentLock, null);
        heldLock?.Dispose();
    }
}

/// <summary>
/// Lifecycle guard for one command-context undo unit. AutoCAD exposes no managed undo-mark API, so
/// this boundary uses the synchronous, typed <see cref="Autodesk.AutoCAD.EditorInput.Editor.Command"/>
/// API with one fixed UNDO Begin/End pair. Business geometry never passes through command strings.
/// </summary>
public sealed class AutoCadCommandContextUndoGroup : IUndoGroup
{
    public const bool RequiresLiveUndoGroupingVerification = false;

    private readonly Autodesk.AutoCAD.EditorInput.Editor _editor;
    private UndoGroupState _state = UndoGroupState.Active;

    public AutoCadCommandContextUndoGroup(Document document)
    {
        ArgumentNullException.ThrowIfNull(document);
        _editor = document.Editor;
        _editor.Command("_.UNDO", "_BEGIN");
    }

    public void Complete()
    {
        EnsureActive();
        EndUndoGroup();
        _state = UndoGroupState.Completed;
    }

    public void Rollback()
    {
        EnsureActive();
        // The caller aborts the only transaction before this method. Closing the empty group is
        // therefore the rollback operation; issuing UNDO here could undo unrelated user work.
        EndUndoGroup();
        _state = UndoGroupState.RolledBack;
    }

    public void Dispose()
    {
        if (_state == UndoGroupState.Disposed)
        {
            return;
        }

        try
        {
            if (_state == UndoGroupState.Active)
            {
                // A post-commit observer can fail after the commit boundary. Always close the
                // group while preserving the caller's UNKNOWN_COMMIT_STATE classification.
                EndUndoGroup();
            }
        }
        finally
        {
            _state = UndoGroupState.Disposed;
        }
    }

    private void EndUndoGroup() => _editor.Command("_.UNDO", "_END");

    private void EnsureActive()
    {
        if (_state != UndoGroupState.Active)
        {
            throw new InvalidOperationException("The AutoCAD undo-group lifecycle is no longer active.");
        }
    }

    private enum UndoGroupState
    {
        Active,
        Completed,
        RolledBack,
        Disposed,
    }
}

/// <summary>
/// Caller-owned AutoCAD transaction used for geometry and metadata together. It never creates a
/// nested transaction and exposes transaction control only through <see cref="IAtomicTransaction"/>.
/// </summary>
public sealed class AutoCadAtomicTransaction : IAtomicTransaction, IActiveMetadataTransactionAccess
{
    private readonly Database _database;
    private Transaction? _transaction;
    private AtomicTransactionState _state = AtomicTransactionState.Active;

    public AutoCadAtomicTransaction(Database database, Transaction transaction)
    {
        ArgumentNullException.ThrowIfNull(database);
        ArgumentNullException.ThrowIfNull(transaction);
        _database = database;
        _transaction = transaction;
    }

    public bool IsActive => _state == AtomicTransactionState.Active && _transaction is not null;

    internal Database Database
    {
        get
        {
            EnsureActive();
            return _database;
        }
    }

    internal Transaction Transaction
    {
        get
        {
            EnsureActive();
            return _transaction!;
        }
    }

    public void Commit()
    {
        EnsureActive();
        _transaction!.Commit();
        _state = AtomicTransactionState.Committed;
    }

    public void Abort()
    {
        EnsureActive();
        _transaction!.Abort();
        _state = AtomicTransactionState.Aborted;
    }

    public void Dispose()
    {
        var transaction = Interlocked.Exchange(ref _transaction, null);
        if (transaction is null)
        {
            return;
        }

        transaction.Dispose();
        _state = AtomicTransactionState.Disposed;
    }

    private void EnsureActive()
    {
        if (!IsActive)
        {
            throw new InvalidOperationException("The AutoCAD transaction is not active.");
        }
    }

    private enum AtomicTransactionState
    {
        Active,
        Committed,
        Aborted,
        Disposed,
    }
}

/// <summary>Opaque metadata reference backed by one valid AutoCAD object id.</summary>
public sealed class AutoCadMetadataEntityReference : IMetadataEntityReference
{
    public AutoCadMetadataEntityReference(ObjectId objectId)
    {
        if (objectId.IsNull || !objectId.IsValid)
        {
            throw new ArgumentException("Metadata requires a valid AutoCAD object id.", nameof(objectId));
        }

        ObjectId = objectId;
    }

    internal ObjectId ObjectId { get; }
}

/// <summary>Fixed-schema CADHARNESS XData reader/writer using the caller's active transaction.</summary>
public sealed class AutoCadXDataMetadataWriter : IMetadataWriter
{
    private const string FeatureField = "feature_id";
    private const string OperationField = "operation_id";

    public ValueTask AttachAsync(
        IActiveMetadataTransactionAccess activeTransaction,
        IMetadataEntityReference newlyCreatedEntity,
        CadHarnessMetadata metadata,
        CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(metadata);
        var (transaction, entityReference) = RequireTypedAccess(
            activeTransaction,
            newlyCreatedEntity);
        cancellationToken.ThrowIfCancellationRequested();
        EnsureRegisteredApplication(transaction);

        var entity = transaction.Transaction.GetObject(
            entityReference.ObjectId,
            OpenMode.ForWrite);
        using var xdata = new ResultBuffer(
            new TypedValue(
                (int)DxfCode.ExtendedDataRegAppName,
                CadHarnessMetadataRegistry.ApplicationName),
            new TypedValue((int)DxfCode.ExtendedDataAsciiString, FeatureField),
            new TypedValue((int)DxfCode.ExtendedDataAsciiString, metadata.FeatureId),
            new TypedValue((int)DxfCode.ExtendedDataAsciiString, OperationField),
            new TypedValue((int)DxfCode.ExtendedDataAsciiString, metadata.OperationId));
        cancellationToken.ThrowIfCancellationRequested();
        entity.XData = xdata;
        return ValueTask.CompletedTask;
    }

    public ValueTask<CadHarnessMetadata?> ReadAsync(
        IActiveMetadataTransactionAccess activeTransaction,
        IMetadataEntityReference entity,
        CancellationToken cancellationToken)
    {
        var (transaction, entityReference) = RequireTypedAccess(activeTransaction, entity);
        cancellationToken.ThrowIfCancellationRequested();
        var databaseObject = transaction.Transaction.GetObject(
            entityReference.ObjectId,
            OpenMode.ForRead);
        using var xdata = databaseObject.GetXDataForApplication(
            CadHarnessMetadataRegistry.ApplicationName);
        if (xdata is null)
        {
            return ValueTask.FromResult<CadHarnessMetadata?>(null);
        }

        var values = xdata.AsArray();
        if (values.Length != 5 ||
            !IsValue(values[0], DxfCode.ExtendedDataRegAppName, CadHarnessMetadataRegistry.ApplicationName) ||
            !IsValue(values[1], DxfCode.ExtendedDataAsciiString, FeatureField) ||
            !TryString(values[2], DxfCode.ExtendedDataAsciiString, out var featureId) ||
            !IsValue(values[3], DxfCode.ExtendedDataAsciiString, OperationField) ||
            !TryString(values[4], DxfCode.ExtendedDataAsciiString, out var operationId))
        {
            throw new InvalidOperationException("CADHARNESS metadata does not match the fixed XData schema.");
        }

        cancellationToken.ThrowIfCancellationRequested();
        return ValueTask.FromResult<CadHarnessMetadata?>(
            CadHarnessMetadata.Create(featureId, operationId));
    }

    private static void EnsureRegisteredApplication(AutoCadAtomicTransaction transaction)
    {
        var table = (RegAppTable)transaction.Transaction.GetObject(
            transaction.Database.RegAppTableId,
            OpenMode.ForRead);
        if (table.Has(CadHarnessMetadataRegistry.ApplicationName))
        {
            return;
        }

        table.UpgradeOpen();
        var record = new RegAppTableRecord
        {
            Name = CadHarnessMetadataRegistry.ApplicationName,
        };
        table.Add(record);
        transaction.Transaction.AddNewlyCreatedDBObject(record, true);
    }

    private static (AutoCadAtomicTransaction Transaction, AutoCadMetadataEntityReference Entity)
        RequireTypedAccess(
            IActiveMetadataTransactionAccess activeTransaction,
            IMetadataEntityReference entity)
    {
        ArgumentNullException.ThrowIfNull(activeTransaction);
        ArgumentNullException.ThrowIfNull(entity);
        if (activeTransaction is not AutoCadAtomicTransaction transaction || !transaction.IsActive)
        {
            throw new InvalidOperationException(
                "Metadata access requires the caller's active AutoCAD transaction.");
        }

        if (entity is not AutoCadMetadataEntityReference entityReference)
        {
            throw new ArgumentException(
                "Metadata access requires a typed AutoCAD object reference.",
                nameof(entity));
        }

        return (transaction, entityReference);
    }

    private static bool IsValue(TypedValue value, DxfCode code, string expected) =>
        value.TypeCode == (int)code && string.Equals(value.Value as string, expected, StringComparison.Ordinal);

    private static bool TryString(TypedValue value, DxfCode code, out string result)
    {
        result = value.Value as string ?? string.Empty;
        return value.TypeCode == (int)code && result.Length > 0;
    }
}
