using System.Globalization;
using System.Buffers.Binary;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Serialization;
using CadBridge.Metadata;

namespace CadBridge.Inspection;

/// <summary>An opaque, persistent database handle, never an in-memory object id.</summary>
public sealed record StableEntityReference
{
    [JsonConstructor]
    public StableEntityReference(string handle)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(handle);
        if (handle.Length > 64 || handle.Any(character => !Uri.IsHexDigit(character)))
        {
            throw new ArgumentException("A stable entity handle must contain 1-64 hexadecimal characters.", nameof(handle));
        }

        Handle = handle.ToUpperInvariant();
    }

    [JsonPropertyName("handle")]
    public string Handle { get; }
}

public enum InspectionGeometryKind
{
    Point,
    Line,
    Circle,
    Arc,
    Ellipse,
    Polyline,
    Text,
    Dimension,
    Hatch,
    BlockReference,
    Other,
}

public enum InspectionStyleKind
{
    Text,
    Dimension,
    Multileader,
    Table,
    Other,
}

public sealed record InspectionPoint(
    [property: JsonPropertyName("x")] double X,
    [property: JsonPropertyName("y")] double Y,
    [property: JsonPropertyName("z")] double Z = 0.0);

public sealed record InspectionBounds(
    [property: JsonPropertyName("minimum")] InspectionPoint Minimum,
    [property: JsonPropertyName("maximum")] InspectionPoint Maximum);

/// <summary>
/// Observable geometry copied from the bound AutoCAD database entity. Extents, when supplied, must be
/// database extents read by the Autodesk-facing adapter rather than client expectations.
/// </summary>
public sealed record InspectionGeometry(
    [property: JsonPropertyName("kind")] InspectionGeometryKind Kind,
    [property: JsonPropertyName("points")] IReadOnlyList<InspectionPoint> Points,
    [property: JsonPropertyName("bounds")] InspectionBounds? Bounds = null,
    [property: JsonPropertyName("radius")] double? Radius = null,
    [property: JsonPropertyName("start_angle_radians")] double? StartAngleRadians = null,
    [property: JsonPropertyName("end_angle_radians")] double? EndAngleRadians = null,
    [property: JsonPropertyName("closed")] bool Closed = false,
    [property: JsonPropertyName("bulges")] IReadOnlyList<double>? Bulges = null,
    [property: JsonPropertyName("major_axis")] double? MajorAxis = null,
    [property: JsonPropertyName("minor_axis")] double? MinorAxis = null,
    [property: JsonPropertyName("rotation_radians")] double? RotationRadians = null,
    [property: JsonPropertyName("text_height")] double? TextHeight = null,
    [property: JsonPropertyName("text_style")] string? TextStyle = null,
    [property: JsonPropertyName("dimension_type")] string? DimensionType = null,
    [property: JsonPropertyName("dimension_style")] string? DimensionStyle = null,
    [property: JsonPropertyName("measurement")] double? Measurement = null,
    [property: JsonPropertyName("text_override")] string? TextOverride = null,
    [property: JsonPropertyName("measured_entity_refs")] IReadOnlyList<StableEntityReference>? MeasuredEntityRefs = null,
    [property: JsonPropertyName("pattern_name")] string? PatternName = null,
    [property: JsonPropertyName("observed_area")] double? ObservedArea = null,
    [property: JsonPropertyName("boundary_entity_refs")] IReadOnlyList<StableEntityReference>? BoundaryEntityRefs = null,
    [property: JsonPropertyName("block_name")] string? BlockName = null,
    [property: JsonPropertyName("insertion")] InspectionPoint? Insertion = null,
    [property: JsonPropertyName("scale_x")] double? ScaleX = null,
    [property: JsonPropertyName("scale_y")] double? ScaleY = null,
    [property: JsonPropertyName("non_uniform_scale")] bool NonUniformScale = false,
    [property: JsonPropertyName("nested_depth_read")] int NestedDepthRead = 0,
    [property: JsonPropertyName("child_entities")] IReadOnlyList<InspectionEntity>? ChildEntities = null,
    [property: JsonPropertyName("children_beyond_depth")] int ChildrenBeyondDepth = 0);

public sealed record InspectionEntity(
    [property: JsonPropertyName("entity_ref")] StableEntityReference EntityRef,
    [property: JsonPropertyName("entity_type")] string EntityType,
    [property: JsonPropertyName("layer")] string Layer,
    [property: JsonPropertyName("style")] string? Style,
    [property: JsonPropertyName("color_index")] short? ColorIndex,
    [property: JsonPropertyName("linetype")] string? Linetype,
    [property: JsonPropertyName("content")] string? Content,
    [property: JsonPropertyName("geometry")] InspectionGeometry Geometry,
    [property: JsonPropertyName("harness_metadata")] CadHarnessMetadata? HarnessMetadata = null,
    [property: JsonPropertyName("visible")] bool Visible = true,
    [property: JsonPropertyName("space")] string Space = "model_space");

public sealed record InspectionLayer(
    [property: JsonPropertyName("name")] string Name,
    [property: JsonPropertyName("is_off")] bool IsOff,
    [property: JsonPropertyName("is_frozen")] bool IsFrozen,
    [property: JsonPropertyName("is_locked")] bool IsLocked,
    [property: JsonPropertyName("color_index")] short? ColorIndex,
    [property: JsonPropertyName("linetype")] string? Linetype);

public sealed record InspectionStyle(
    [property: JsonPropertyName("name")] string Name,
    [property: JsonPropertyName("kind")] InspectionStyleKind Kind,
    [property: JsonPropertyName("annotative")] bool Annotative,
    [property: JsonPropertyName("scale")] double? Scale);

public sealed record DocumentInspectionSnapshot(
    [property: JsonPropertyName("document_id")] string DocumentId,
    [property: JsonPropertyName("revision")] string Revision,
    [property: JsonPropertyName("source_unit_code")] string SourceUnitCode,
    [property: JsonPropertyName("entities")] IReadOnlyList<InspectionEntity> Entities,
    [property: JsonPropertyName("layers")] IReadOnlyList<InspectionLayer> Layers,
    [property: JsonPropertyName("styles")] IReadOnlyList<InspectionStyle> Styles);

public sealed record SelectionInspectionSnapshot(
    [property: JsonPropertyName("document_id")] string DocumentId,
    [property: JsonPropertyName("revision")] string Revision,
    [property: JsonPropertyName("entities")] IReadOnlyList<InspectionEntity> Entities);

public sealed record ActualEntityMeasurement(
    [property: JsonPropertyName("entity_ref")] StableEntityReference EntityRef,
    [property: JsonPropertyName("geometry_kind")] InspectionGeometryKind GeometryKind,
    [property: JsonPropertyName("length")] double? Length,
    [property: JsonPropertyName("area")] double? Area,
    [property: JsonPropertyName("radius")] double? Radius,
    [property: JsonPropertyName("bounds")] InspectionBounds? Bounds);

public sealed record CreatedEntityMeasurementSnapshot(
    [property: JsonPropertyName("document_id")] string DocumentId,
    [property: JsonPropertyName("revision")] string Revision,
    [property: JsonPropertyName("measurements")] IReadOnlyList<ActualEntityMeasurement> Measurements);

/// <summary>
/// Read-only view of exactly one already-bound document. Autodesk-dependent code implements this
/// interface from database objects; deliberately absent are document selection, command execution,
/// mutation, lock, and transaction lifecycle members.
/// </summary>
public interface IBoundInspectionDocument
{
    string DocumentId { get; }

    /// <summary>
    /// Stable source drawing unit code. Autodesk-bound implementations must override this default
    /// with the observed database unit; the default exists only for compatibility with older fakes.
    /// </summary>
    string SourceUnitCode => "unitless";

    IEnumerable<InspectionEntity> ReadEntities();

    /// <summary>
    /// Enumerates observable entities while allowing Autodesk-facing implementations to stop
    /// expensive per-entity geometry reads before materializing an unbounded payload.
    /// Existing read-only implementations retain the parameterless contract by default.
    /// </summary>
    IEnumerable<InspectionEntity> ReadEntities(CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        return ReadEntities();
    }

    IEnumerable<InspectionLayer> ReadLayers();

    IEnumerable<InspectionStyle> ReadStyles();

    IReadOnlyCollection<StableEntityReference> ReadSelection();
}

/// <summary>Optional host port that can stop before materializing an oversized selection.</summary>
public interface IBoundedSelectionInspectionDocument
{
    IReadOnlyCollection<StableEntityReference> ReadSelection(int maxEntities);
}

/// <summary>Provides deterministic, read-only inspection and post-write measurement.</summary>
public sealed class BridgeInspectionService
{
    public const int MaximumWholeDocumentObservationEntities = 200_000;
    private const double FullTurn = Math.PI * 2.0;
    private const int MaximumNestedEntityDepth = 32;
    private readonly IBoundInspectionDocument _document;
    private readonly string _documentId;
    private readonly string _sourceUnitCode;

    public BridgeInspectionService(IBoundInspectionDocument document)
    {
        ArgumentNullException.ThrowIfNull(document);
        ValidateIdentifier(document.DocumentId, nameof(document.DocumentId));
        _document = document;
        _documentId = document.DocumentId;
        _sourceUnitCode = NormalizeSourceUnitCode(document.SourceUnitCode);
    }

    public DocumentInspectionSnapshot InspectDocument(CancellationToken cancellationToken = default)
    {
        var entities = ReadAndOrderEntities(cancellationToken);
        var layers = ReadAndOrderLayers(cancellationToken);
        var styles = ReadAndOrderStyles(cancellationToken);
        var revision = ComputeRevision(
            _documentId,
            _sourceUnitCode,
            entities,
            layers,
            styles,
            cancellationToken);
        return new DocumentInspectionSnapshot(
            _documentId,
            revision,
            _sourceUnitCode,
            entities,
            layers,
            styles);
    }

    /// <summary>
    /// Computes a whole-document revision while retaining only the bounded entities accepted by
    /// <paramref name="includeEntity"/>. Non-matching geometry is canonicalized one entity at a
    /// time and is never accumulated in the returned snapshot.
    /// </summary>
    public DocumentInspectionSnapshot InspectDocumentBounded(
        int maxEntities,
        Func<InspectionEntity, bool> includeEntity,
        CancellationToken cancellationToken = default)
    {
        ArgumentOutOfRangeException.ThrowIfNegativeOrZero(maxEntities);
        ArgumentNullException.ThrowIfNull(includeEntity);
        var layers = ReadAndOrderLayers(cancellationToken);
        var styles = ReadAndOrderStyles(cancellationToken);
        var entities = new List<InspectionEntity>(Math.Min(maxEntities, 1024));
        var handles = new HashSet<string>(StringComparer.Ordinal);
        var revisionEntities = new EntityRevisionAccumulator();
        var observedEntityCount = 0;
        foreach (var source in _document.ReadEntities(cancellationToken))
        {
            cancellationToken.ThrowIfCancellationRequested();
            observedEntityCount = checked(observedEntityCount + 1);
            if (observedEntityCount > MaximumWholeDocumentObservationEntities)
            {
                throw new InvalidOperationException(
                    $"The document exceeds the hard observation limit of {MaximumWholeDocumentObservationEntities} entities.");
            }

            var entity = CopyAndValidate(source);
            if (!handles.Add(entity.EntityRef.Handle))
            {
                throw new InvalidOperationException("The bound document returned a duplicate stable entity handle.");
            }

            revisionEntities.Add(entity, cancellationToken);
            if (!includeEntity(entity))
            {
                continue;
            }

            if (entities.Count == maxEntities)
            {
                throw new InvalidOperationException("The requested scope exceeds max_entities; partial inspection results are forbidden.");
            }

            entities.Add(entity);
        }

        entities.Sort((left, right) => StringComparer.Ordinal.Compare(
            left.EntityRef.Handle,
            right.EntityRef.Handle));
        var revision = ComputeRevision(
            _documentId,
            _sourceUnitCode,
            revisionEntities,
            layers,
            styles,
            cancellationToken);
        return new DocumentInspectionSnapshot(
            _documentId,
            revision,
            _sourceUnitCode,
            entities,
            layers,
            styles);
    }

    public SelectionInspectionSnapshot InspectSelection(CancellationToken cancellationToken = default)
    {
        var document = InspectDocument(cancellationToken);
        var selectedHandles = _document.ReadSelection()
            .Select(reference => reference.Handle)
            .ToHashSet(StringComparer.Ordinal);
        var selection = new List<InspectionEntity>(selectedHandles.Count);
        foreach (var entity in document.Entities)
        {
            cancellationToken.ThrowIfCancellationRequested();
            if (selectedHandles.Contains(entity.EntityRef.Handle))
            {
                selection.Add(entity);
            }
        }

        return new SelectionInspectionSnapshot(document.DocumentId, document.Revision, selection);
    }

    public SelectionInspectionSnapshot InspectSelection(
        int maxEntities,
        CancellationToken cancellationToken = default)
    {
        ArgumentOutOfRangeException.ThrowIfNegativeOrZero(maxEntities);
        var selectedHandles = ReadBoundedSelection(maxEntities)
            .Select(reference => reference.Handle)
            .ToHashSet(StringComparer.Ordinal);
        var document = InspectDocumentBounded(
            maxEntities,
            entity => selectedHandles.Contains(entity.EntityRef.Handle),
            cancellationToken);
        if (document.Entities.Count != selectedHandles.Count)
        {
            throw new InvalidOperationException("The selection contains an entity outside the bound document.");
        }

        return new SelectionInspectionSnapshot(document.DocumentId, document.Revision, document.Entities);
    }

    private IReadOnlyCollection<StableEntityReference> ReadBoundedSelection(int maxEntities)
    {
        var selection = _document is IBoundedSelectionInspectionDocument bounded
            ? bounded.ReadSelection(maxEntities)
            : _document.ReadSelection();
        if (selection.Count > maxEntities)
        {
            throw new InvalidOperationException("The selection exceeds max_entities.");
        }

        return selection;
    }

    /// <summary>
    /// Re-reads the bound document and derives measurements solely from the observed entity geometry.
    /// The API intentionally accepts no expected dimensions, preventing expected-value echo.
    /// </summary>
    public CreatedEntityMeasurementSnapshot MeasureCreatedEntities(
        IReadOnlyCollection<StableEntityReference> createdEntities,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(createdEntities);
        var snapshot = InspectDocument(cancellationToken);
        var byHandle = snapshot.Entities.ToDictionary(entity => entity.EntityRef.Handle, StringComparer.Ordinal);
        var requested = createdEntities
            .Select(reference => reference ?? throw new ArgumentException("Entity references cannot contain null.", nameof(createdEntities)))
            .OrderBy(reference => reference.Handle, StringComparer.Ordinal)
            .ToArray();
        if (requested.Select(reference => reference.Handle).Distinct(StringComparer.Ordinal).Count() != requested.Length)
        {
            throw new ArgumentException("Entity references must be unique.", nameof(createdEntities));
        }

        var measurements = new List<ActualEntityMeasurement>(requested.Length);
        foreach (var reference in requested)
        {
            cancellationToken.ThrowIfCancellationRequested();
            if (!byHandle.TryGetValue(reference.Handle, out var entity))
            {
                throw new KeyNotFoundException($"Entity handle {reference.Handle} is not present in the bound document.");
            }

            measurements.Add(Measure(entity));
        }

        return new CreatedEntityMeasurementSnapshot(snapshot.DocumentId, snapshot.Revision, measurements);
    }

    private InspectionEntity[] ReadAndOrderEntities(CancellationToken cancellationToken)
    {
        var entities = new List<InspectionEntity>();
        var handles = new HashSet<string>(StringComparer.Ordinal);
        foreach (var source in _document.ReadEntities(cancellationToken))
        {
            cancellationToken.ThrowIfCancellationRequested();
            if (entities.Count == MaximumWholeDocumentObservationEntities)
            {
                throw new InvalidOperationException(
                    $"The document exceeds the hard observation limit of {MaximumWholeDocumentObservationEntities} entities.");
            }

            var entity = CopyAndValidate(source);
            if (!handles.Add(entity.EntityRef.Handle))
            {
                throw new InvalidOperationException("The bound document returned a duplicate stable entity handle.");
            }

            entities.Add(entity);
        }

        return entities.OrderBy(entity => entity.EntityRef.Handle, StringComparer.Ordinal).ToArray();
    }

    private InspectionLayer[] ReadAndOrderLayers(CancellationToken cancellationToken)
    {
        var layers = new List<InspectionLayer>();
        var names = new HashSet<string>(StringComparer.Ordinal);
        foreach (var source in _document.ReadLayers())
        {
            cancellationToken.ThrowIfCancellationRequested();
            var layer = CopyAndValidate(source);
            if (!names.Add(layer.Name))
            {
                throw new InvalidOperationException("The bound document returned a duplicate layer name.");
            }

            layers.Add(layer);
        }

        return layers.OrderBy(layer => layer.Name, StringComparer.Ordinal).ToArray();
    }

    private InspectionStyle[] ReadAndOrderStyles(CancellationToken cancellationToken)
    {
        var styles = new List<InspectionStyle>();
        var keys = new HashSet<(InspectionStyleKind Kind, string Name)>();
        foreach (var source in _document.ReadStyles())
        {
            cancellationToken.ThrowIfCancellationRequested();
            var style = CopyAndValidate(source);
            if (!keys.Add((style.Kind, style.Name)))
            {
                throw new InvalidOperationException("The bound document returned a duplicate style identity.");
            }

            styles.Add(style);
        }

        return styles.OrderBy(style => style.Kind).ThenBy(style => style.Name, StringComparer.Ordinal).ToArray();
    }

    private static InspectionEntity CopyAndValidate(InspectionEntity source) =>
        CopyAndValidate(
            source,
            0,
            new HashSet<InspectionEntity>(ReferenceEqualityComparer.Instance));

    private static InspectionEntity CopyAndValidate(
        InspectionEntity source,
        int depth,
        HashSet<InspectionEntity> ancestors)
    {
        ArgumentNullException.ThrowIfNull(source);
        if (depth > MaximumNestedEntityDepth)
        {
            throw new InvalidOperationException("Nested inspection entities exceed the safe depth limit.");
        }

        if (!ancestors.Add(source))
        {
            throw new InvalidOperationException("Nested inspection entities contain a cycle.");
        }

        try
        {
            ArgumentNullException.ThrowIfNull(source.EntityRef);
            ValidateIdentifier(source.EntityType, nameof(source.EntityType));
            ValidateIdentifier(source.Layer, nameof(source.Layer));
            ValidateOptionalIdentifier(source.Style, nameof(source.Style));
            ValidateOptionalIdentifier(source.Linetype, nameof(source.Linetype));
            ValidateIdentifier(source.Space, nameof(source.Space));
            var geometry = CopyAndValidate(source.Geometry, depth, ancestors);
            return source with { Geometry = geometry };
        }
        finally
        {
            ancestors.Remove(source);
        }
    }

    private static InspectionLayer CopyAndValidate(InspectionLayer source)
    {
        ArgumentNullException.ThrowIfNull(source);
        ValidateIdentifier(source.Name, "layer.Name");
        ValidateOptionalIdentifier(source.Linetype, nameof(source.Linetype));
        return source;
    }

    private static InspectionStyle CopyAndValidate(InspectionStyle source)
    {
        ArgumentNullException.ThrowIfNull(source);
        ValidateIdentifier(source.Name, "style.Name");
        ValidateFinite(source.Scale, nameof(source.Scale));
        return source;
    }

    private static InspectionGeometry CopyAndValidate(
        InspectionGeometry source,
        int entityDepth,
        HashSet<InspectionEntity> ancestors)
    {
        ArgumentNullException.ThrowIfNull(source);
        ArgumentNullException.ThrowIfNull(source.Points);
        var points = source.Points.Select(CopyAndValidate).ToArray();
        ValidateFinite(source.Radius, nameof(source.Radius));
        ValidateFinite(source.StartAngleRadians, nameof(source.StartAngleRadians));
        ValidateFinite(source.EndAngleRadians, nameof(source.EndAngleRadians));
        ValidateFinite(source.MajorAxis, nameof(source.MajorAxis));
        ValidateFinite(source.MinorAxis, nameof(source.MinorAxis));
        ValidateFinite(source.RotationRadians, nameof(source.RotationRadians));
        ValidateFinite(source.TextHeight, nameof(source.TextHeight));
        ValidateFinite(source.Measurement, nameof(source.Measurement));
        ValidateFinite(source.ObservedArea, nameof(source.ObservedArea));
        ValidateFinite(source.ScaleX, nameof(source.ScaleX));
        ValidateFinite(source.ScaleY, nameof(source.ScaleY));
        if (source.Radius is <= 0.0)
        {
            throw new ArgumentOutOfRangeException(nameof(source), "A geometry radius must be positive.");
        }

        if (source.MajorAxis is <= 0.0 || source.MinorAxis is <= 0.0 ||
            source.TextHeight is <= 0.0 || source.ScaleX is 0.0 || source.ScaleY is 0.0 ||
            source.ObservedArea is < 0.0 || source.NestedDepthRead < 0 || source.ChildrenBeyondDepth < 0)
        {
            throw new ArgumentOutOfRangeException(nameof(source), "Observed geometry dimensions and counts are invalid.");
        }

        ValidateOptionalIdentifier(source.TextStyle, nameof(source.TextStyle));
        ValidateOptionalIdentifier(source.DimensionType, nameof(source.DimensionType));
        ValidateOptionalIdentifier(source.DimensionStyle, nameof(source.DimensionStyle));
        ValidateOptionalIdentifier(source.PatternName, nameof(source.PatternName));
        ValidateOptionalIdentifier(source.BlockName, nameof(source.BlockName));

        var bounds = source.Bounds is null ? null : CopyAndValidate(source.Bounds);
        var bulges = source.Bulges?.ToArray();
        if (bulges is not null)
        {
            if (bulges.Length != points.Length)
            {
                throw new ArgumentException("Polyline bulges must have one value per observed vertex.", nameof(source));
            }

            foreach (var bulge in bulges)
            {
                ValidateFinite(bulge, nameof(source.Bulges));
            }
        }

        var insertion = source.Insertion is null ? null : CopyAndValidate(source.Insertion);
        var measuredEntityRefs = CopyReferences(source.MeasuredEntityRefs, nameof(source.MeasuredEntityRefs));
        var boundaryEntityRefs = CopyReferences(source.BoundaryEntityRefs, nameof(source.BoundaryEntityRefs));
        InspectionEntity[]? childEntities = null;
        if (source.ChildEntities is not null)
        {
            var childHandles = new HashSet<string>(StringComparer.Ordinal);
            childEntities = source.ChildEntities
                .Select(child => CopyAndValidate(child, entityDepth + 1, ancestors))
                .OrderBy(child => child.EntityRef.Handle, StringComparer.Ordinal)
                .ToArray();
            if (childEntities.Any(child => !childHandles.Add(child.EntityRef.Handle)))
            {
                throw new InvalidOperationException(
                    "Nested inspection entities contain a duplicate stable entity handle.");
            }
        }
        ValidateGeometryShape(source, points, bulges);
        return source with
        {
            Points = points,
            Bounds = bounds,
            Bulges = bulges,
            Insertion = insertion,
            MeasuredEntityRefs = measuredEntityRefs,
            BoundaryEntityRefs = boundaryEntityRefs,
            ChildEntities = childEntities,
        };
    }

    private static InspectionPoint CopyAndValidate(InspectionPoint source)
    {
        ArgumentNullException.ThrowIfNull(source);
        ValidateFinite(source.X, nameof(source.X));
        ValidateFinite(source.Y, nameof(source.Y));
        ValidateFinite(source.Z, nameof(source.Z));
        return source;
    }

    private static InspectionBounds CopyAndValidate(InspectionBounds source)
    {
        ArgumentNullException.ThrowIfNull(source);
        var minimum = CopyAndValidate(source.Minimum);
        var maximum = CopyAndValidate(source.Maximum);
        if (minimum.X > maximum.X || minimum.Y > maximum.Y || minimum.Z > maximum.Z)
        {
            throw new ArgumentException("Geometry bounds must be ordered minimum-to-maximum.", nameof(source));
        }

        return new InspectionBounds(minimum, maximum);
    }

    private static StableEntityReference[]? CopyReferences(
        IReadOnlyList<StableEntityReference>? references,
        string parameterName)
    {
        if (references is null)
        {
            return null;
        }

        if (references.Any(reference => reference is null) ||
            references.Select(reference => reference.Handle).Distinct(StringComparer.Ordinal).Count() != references.Count)
        {
            throw new ArgumentException("Observed entity references must be non-null and unique.", parameterName);
        }

        return references.Select(reference => new StableEntityReference(reference.Handle)).ToArray();
    }

    private static void ValidateGeometryShape(
        InspectionGeometry source,
        IReadOnlyCollection<InspectionPoint> points,
        IReadOnlyList<double>? bulges)
    {
        var valid = source.Kind switch
        {
            InspectionGeometryKind.Point => points.Count == 1,
            InspectionGeometryKind.Line => points.Count == 2,
            InspectionGeometryKind.Circle => points.Count == 1 && source.Radius.HasValue,
            InspectionGeometryKind.Arc => points.Count == 1 && source.Radius.HasValue &&
                source.StartAngleRadians.HasValue && source.EndAngleRadians.HasValue,
            InspectionGeometryKind.Ellipse => points.Count == 1 && source.MajorAxis.HasValue &&
                source.MinorAxis.HasValue && source.RotationRadians.HasValue,
            InspectionGeometryKind.Polyline => points.Count >= 2 &&
                (bulges is null || bulges.Count == points.Count),
            InspectionGeometryKind.Text => points.Count >= 1 && source.TextHeight.HasValue &&
                source.TextStyle is not null,
            InspectionGeometryKind.Dimension => source.DimensionType is not null &&
                source.DimensionStyle is not null,
            InspectionGeometryKind.Hatch => source.PatternName is not null,
            InspectionGeometryKind.BlockReference => source.BlockName is not null &&
                source.Insertion is not null && source.ScaleX.HasValue && source.ScaleY.HasValue,
            InspectionGeometryKind.Other => true,
            _ => false,
        };
        if (!valid)
        {
            throw new ArgumentException("Geometry does not contain the observations required for its kind.", nameof(source));
        }
    }

    private static ActualEntityMeasurement Measure(InspectionEntity entity)
    {
        var geometry = entity.Geometry;
        var length = geometry.Kind switch
        {
            InspectionGeometryKind.Line => Distance(geometry.Points[0], geometry.Points[1]),
            InspectionGeometryKind.Circle => FullTurn * geometry.Radius!.Value,
            InspectionGeometryKind.Arc => ArcSweep(geometry.StartAngleRadians!.Value, geometry.EndAngleRadians!.Value) * geometry.Radius!.Value,
            InspectionGeometryKind.Ellipse => EllipseCircumference(
                geometry.MajorAxis!.Value,
                geometry.MinorAxis!.Value),
            InspectionGeometryKind.Polyline => PolylineLength(
                geometry.Points,
                geometry.Bulges,
                geometry.Closed),
            _ => (double?)null,
        };
        var area = geometry.Kind switch
        {
            InspectionGeometryKind.Circle => Math.PI * geometry.Radius!.Value * geometry.Radius.Value,
            InspectionGeometryKind.Ellipse => Math.PI * geometry.MajorAxis!.Value * geometry.MinorAxis!.Value,
            InspectionGeometryKind.Polyline when geometry.Closed => PolylineArea(
                geometry.Points,
                geometry.Bulges),
            InspectionGeometryKind.Hatch => geometry.ObservedArea,
            _ => (double?)null,
        };
        var bounds = geometry.Bounds ?? DerivedBounds(geometry);
        return new ActualEntityMeasurement(entity.EntityRef, geometry.Kind, length, area, geometry.Radius, bounds);
    }

    private static double Distance(InspectionPoint left, InspectionPoint right)
    {
        var dx = right.X - left.X;
        var dy = right.Y - left.Y;
        var dz = right.Z - left.Z;
        return Math.Sqrt((dx * dx) + (dy * dy) + (dz * dz));
    }

    private static double PolylineLength(
        IReadOnlyList<InspectionPoint> points,
        IReadOnlyList<double>? bulges,
        bool closed)
    {
        var length = 0.0;
        var segmentCount = closed ? points.Count : points.Count - 1;
        for (var index = 0; index < segmentCount; index++)
        {
            var next = (index + 1) % points.Count;
            var chord = Distance(points[index], points[next]);
            length += ArcSegmentLength(chord, bulges?[index] ?? 0.0);
        }

        return length;
    }

    private static double PolylineArea(
        IReadOnlyList<InspectionPoint> points,
        IReadOnlyList<double>? bulges)
    {
        var twiceArea = 0.0;
        var curvedArea = 0.0;
        for (var index = 0; index < points.Count; index++)
        {
            var next = (index + 1) % points.Count;
            twiceArea += (points[index].X * points[next].Y) - (points[next].X * points[index].Y);
            curvedArea += ArcSegmentSignedArea(
                Distance(points[index], points[next]),
                bulges?[index] ?? 0.0);
        }

        return Math.Abs((twiceArea / 2.0) + curvedArea);
    }

    private static double ArcSegmentLength(double chord, double bulge)
    {
        if (Math.Abs(bulge) <= double.Epsilon || chord <= double.Epsilon)
        {
            return chord;
        }

        var includedAngle = 4.0 * Math.Atan(bulge);
        var radius = chord * (1.0 + (bulge * bulge)) / (4.0 * Math.Abs(bulge));
        return Math.Abs(includedAngle) * radius;
    }

    private static double ArcSegmentSignedArea(double chord, double bulge)
    {
        if (Math.Abs(bulge) <= double.Epsilon || chord <= double.Epsilon)
        {
            return 0.0;
        }

        var includedAngle = 4.0 * Math.Atan(bulge);
        var radius = chord * (1.0 + (bulge * bulge)) / (4.0 * Math.Abs(bulge));
        return 0.5 * radius * radius * (includedAngle - Math.Sin(includedAngle));
    }

    private static double EllipseCircumference(double majorAxis, double minorAxis)
    {
        // Ramanujan's second approximation is stable and more accurate than the first for
        // the eccentricities encountered in mechanical drawings.
        var sum = majorAxis + minorAxis;
        var difference = majorAxis - minorAxis;
        var h = (difference * difference) / (sum * sum);
        return Math.PI * sum * (1.0 + ((3.0 * h) / (10.0 + Math.Sqrt(4.0 - (3.0 * h)))));
    }

    private static double ArcSweep(double startAngle, double endAngle)
    {
        var sweep = (endAngle - startAngle) % FullTurn;
        return sweep < 0.0 ? sweep + FullTurn : sweep;
    }

    private static InspectionBounds? DerivedBounds(InspectionGeometry geometry)
    {
        if (geometry.Kind == InspectionGeometryKind.Circle)
        {
            var center = geometry.Points[0];
            var radius = geometry.Radius!.Value;
            return new InspectionBounds(
                new InspectionPoint(center.X - radius, center.Y - radius, center.Z),
                new InspectionPoint(center.X + radius, center.Y + radius, center.Z));
        }

        if (geometry.Kind == InspectionGeometryKind.Arc)
        {
            return ArcBounds(geometry);
        }

        if (geometry.Points.Count == 0)
        {
            return null;
        }

        return new InspectionBounds(
            new InspectionPoint(geometry.Points.Min(point => point.X), geometry.Points.Min(point => point.Y), geometry.Points.Min(point => point.Z)),
            new InspectionPoint(geometry.Points.Max(point => point.X), geometry.Points.Max(point => point.Y), geometry.Points.Max(point => point.Z)));
    }

    private static InspectionBounds ArcBounds(InspectionGeometry geometry)
    {
        var center = geometry.Points[0];
        var radius = geometry.Radius!.Value;
        var start = geometry.StartAngleRadians!.Value;
        var sweep = ArcSweep(start, geometry.EndAngleRadians!.Value);
        var angles = new List<double> { start, start + sweep };
        foreach (var cardinal in new[] { 0.0, Math.PI / 2.0, Math.PI, Math.PI * 1.5 })
        {
            if (ArcSweep(start, cardinal) <= sweep)
            {
                angles.Add(cardinal);
            }
        }

        var points = angles
            .Select(angle => new InspectionPoint(
                center.X + (radius * Math.Cos(angle)),
                center.Y + (radius * Math.Sin(angle)),
                center.Z))
            .ToArray();
        return new InspectionBounds(
            new InspectionPoint(points.Min(point => point.X), points.Min(point => point.Y), center.Z),
            new InspectionPoint(points.Max(point => point.X), points.Max(point => point.Y), center.Z));
    }

    private static string ComputeRevision(
        string documentId,
        string sourceUnitCode,
        IReadOnlyList<InspectionEntity> entities,
        IReadOnlyList<InspectionLayer> layers,
        IReadOnlyList<InspectionStyle> styles,
        CancellationToken cancellationToken)
    {
        var revisionEntities = new EntityRevisionAccumulator();
        foreach (var entity in entities)
        {
            revisionEntities.Add(entity, cancellationToken);
        }

        return ComputeRevision(
            documentId,
            sourceUnitCode,
            revisionEntities,
            layers,
            styles,
            cancellationToken);
    }

    private static string ComputeRevision(
        string documentId,
        string sourceUnitCode,
        EntityRevisionAccumulator entities,
        IReadOnlyList<InspectionLayer> layers,
        IReadOnlyList<InspectionStyle> styles,
        CancellationToken cancellationToken)
    {
        var canonical = new StringBuilder();
        Append(
            canonical,
            "document",
            documentId,
            "source_unit_code",
            sourceUnitCode,
            "layers",
            layers.Count);
        foreach (var layer in layers)
        {
            Append(canonical, "layer", layer.Name, layer.IsOff, layer.IsFrozen, layer.IsLocked, layer.ColorIndex, layer.Linetype);
        }

        Append(canonical, "styles", styles.Count);
        foreach (var style in styles)
        {
            Append(canonical, "style", style.Name, style.Kind, style.Annotative, style.Scale);
        }

        cancellationToken.ThrowIfCancellationRequested();
        Append(canonical, "entities", entities.Count, "entity_accumulator", entities.Digest);

        var digest = SHA256.HashData(Encoding.UTF8.GetBytes(canonical.ToString()));
        return $"sha256:{Convert.ToHexString(digest).ToLowerInvariant()}";
    }

    /// <summary>
    /// Fixed-memory, order-independent commitment to the complete top-level entity multiset.
    /// Each validated entity is first SHA-256 canonicalized (including its ordered child tree),
    /// then its four 64-bit words are accumulated modulo 2^64. The entity count is committed by
    /// <see cref="ComputeRevision"/>, and duplicate top-level handles are rejected by the caller.
    /// </summary>
    private sealed class EntityRevisionAccumulator
    {
        private readonly ulong[] _words = new ulong[4];

        public long Count { get; private set; }

        public string Digest => string.Concat(_words.Select(word => word.ToString("x16", CultureInfo.InvariantCulture)));

        public void Add(InspectionEntity entity, CancellationToken cancellationToken)
        {
            var canonical = new StringBuilder();
            AppendEntityCanonical(
                canonical,
                entity,
                0,
                new HashSet<InspectionEntity>(ReferenceEqualityComparer.Instance),
                cancellationToken);
            var digest = SHA256.HashData(Encoding.UTF8.GetBytes(canonical.ToString()));
            for (var index = 0; index < _words.Length; index++)
            {
                _words[index] = unchecked(
                    _words[index] + BinaryPrimitives.ReadUInt64BigEndian(digest.AsSpan(index * sizeof(ulong))));
            }

            Count = checked(Count + 1);
        }
    }

    private static void AppendEntityCanonical(
        StringBuilder canonical,
        InspectionEntity entity,
        int depth,
        HashSet<InspectionEntity> ancestors,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        if (depth > MaximumNestedEntityDepth)
        {
            throw new InvalidOperationException("Nested inspection entities exceed the safe depth limit.");
        }

        if (!ancestors.Add(entity))
        {
            throw new InvalidOperationException("Nested inspection entities contain a cycle.");
        }

        try
        {
            Append(
                canonical,
                "entity",
                depth,
                entity.EntityRef.Handle,
                entity.EntityType,
                entity.Layer,
                entity.Style,
                entity.ColorIndex,
                entity.Linetype,
                entity.Content,
                entity.Visible,
                entity.Space);
            var geometry = entity.Geometry;
            Append(
                canonical,
                "geometry",
                geometry.Kind,
                geometry.Radius,
                geometry.StartAngleRadians,
                geometry.EndAngleRadians,
                geometry.Closed,
                geometry.MajorAxis,
                geometry.MinorAxis,
                geometry.RotationRadians,
                geometry.TextHeight,
                geometry.TextStyle,
                geometry.DimensionType,
                geometry.DimensionStyle,
                geometry.Measurement,
                geometry.TextOverride,
                geometry.PatternName,
                geometry.ObservedArea,
                geometry.BlockName,
                geometry.ScaleX,
                geometry.ScaleY,
                geometry.NonUniformScale,
                geometry.NestedDepthRead,
                geometry.ChildrenBeyondDepth,
                geometry.Points.Count);
            foreach (var point in geometry.Points)
            {
                Append(canonical, "point", point.X, point.Y, point.Z);
            }

            Append(canonical, "bulges", geometry.Bulges?.Count ?? 0);
            foreach (var bulge in geometry.Bulges ?? Array.Empty<double>())
            {
                Append(canonical, "bulge", bulge);
            }

            if (geometry.Insertion is { } insertion)
            {
                Append(canonical, "insertion", insertion.X, insertion.Y, insertion.Z);
            }
            else
            {
                Append(canonical, "insertion", (object?)null);
            }

            AppendReferences(canonical, "measured", geometry.MeasuredEntityRefs);
            AppendReferences(canonical, "boundary", geometry.BoundaryEntityRefs);
            if (geometry.Bounds is { } bounds)
            {
                Append(
                    canonical,
                    "bounds",
                    bounds.Minimum.X,
                    bounds.Minimum.Y,
                    bounds.Minimum.Z,
                    bounds.Maximum.X,
                    bounds.Maximum.Y,
                    bounds.Maximum.Z);
            }
            else
            {
                Append(canonical, "bounds", (object?)null);
            }

            Append(
                canonical,
                "metadata",
                entity.HarnessMetadata?.FeatureId,
                entity.HarnessMetadata?.OperationId);
            var children = geometry.ChildEntities ?? Array.Empty<InspectionEntity>();
            Append(canonical, "children", children.Count);
            foreach (var child in children.OrderBy(
                child => child.EntityRef.Handle,
                StringComparer.Ordinal))
            {
                AppendEntityCanonical(
                    canonical,
                    child,
                    depth + 1,
                    ancestors,
                    cancellationToken);
            }
        }
        finally
        {
            ancestors.Remove(entity);
        }
    }

    private static void AppendReferences(
        StringBuilder builder,
        string label,
        IReadOnlyList<StableEntityReference>? references)
    {
        Append(builder, label, references?.Count ?? 0);
        foreach (var reference in references ?? Array.Empty<StableEntityReference>())
        {
            Append(builder, label, reference.Handle);
        }
    }

    private static void Append(StringBuilder builder, params object?[] values)
    {
        foreach (var value in values)
        {
            var text = value switch
            {
                null => "<null>",
                bool boolean => boolean ? "true" : "false",
                double number => number.ToString("R", CultureInfo.InvariantCulture),
                float number => number.ToString("R", CultureInfo.InvariantCulture),
                IFormattable formattable => formattable.ToString(null, CultureInfo.InvariantCulture),
                _ => value.ToString() ?? string.Empty,
            };
            builder.Append(text.Length.ToString(CultureInfo.InvariantCulture)).Append(':').Append(text).Append(';');
        }
    }

    private static void ValidateIdentifier(string value, string parameterName)
    {
        if (string.IsNullOrWhiteSpace(value))
        {
            throw new ArgumentException(
                "Inspection identifiers cannot be blank.",
                $"{parameterName}.blank");
        }

        var invalidReason = value.Length > 256
            ? "length"
            : value.Contains('/')
                ? "slash"
                : value.Contains('\\')
                    ? "backslash"
                    : value.Any(char.IsControl)
                        ? "control"
                        : null;
        if (invalidReason is not null)
        {
            throw new ArgumentException(
                "Inspection identifiers must be bounded and cannot contain paths or control characters.",
                $"{parameterName}.{invalidReason}");
        }
    }

    private static string NormalizeSourceUnitCode(string value)
    {
        ValidateIdentifier(value, nameof(IBoundInspectionDocument.SourceUnitCode));
        return value.Trim().ToLowerInvariant();
    }

    private static void ValidateOptionalIdentifier(string? value, string parameterName)
    {
        if (value is not null)
        {
            ValidateIdentifier(value, parameterName);
        }
    }

    private static void ValidateFinite(double? value, string parameterName)
    {
        if (value.HasValue)
        {
            ValidateFinite(value.Value, parameterName);
        }
    }

    private static void ValidateFinite(double value, string parameterName)
    {
        if (!double.IsFinite(value))
        {
            throw new ArgumentOutOfRangeException(parameterName, "Geometry values must be finite.");
        }
    }
}
