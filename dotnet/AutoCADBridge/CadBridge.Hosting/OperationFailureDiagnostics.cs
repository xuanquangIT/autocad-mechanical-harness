namespace CadBridge.Hosting;

/// <summary>
/// Keeps diagnostic stages request-local while publishing only completed failures globally.
/// Successful concurrent requests never erase a failure produced by another request.
/// </summary>
public sealed class OperationFailureDiagnostics
{
    private string? _lastFailure;

    public string? LastFailure => Volatile.Read(ref _lastFailure);

    public Request Begin(string initialStage) => new(this, initialStage);

    public sealed class Request
    {
        private readonly OperationFailureDiagnostics _owner;
        private string _stage;

        internal Request(OperationFailureDiagnostics owner, string initialStage)
        {
            ArgumentNullException.ThrowIfNull(owner);
            ArgumentException.ThrowIfNullOrWhiteSpace(initialStage);
            _owner = owner;
            _stage = initialStage;
        }

        public string Stage => Volatile.Read(ref _stage);

        public void RecordStage(string stage)
        {
            ArgumentException.ThrowIfNullOrWhiteSpace(stage);
            Volatile.Write(ref _stage, stage);
        }

        public void PublishFailure(string label)
        {
            ArgumentException.ThrowIfNullOrWhiteSpace(label);
            Interlocked.Exchange(ref _owner._lastFailure, label);
        }
    }
}
