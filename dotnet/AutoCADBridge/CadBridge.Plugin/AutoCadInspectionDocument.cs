using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.EditorInput;
using Autodesk.AutoCAD.Geometry;
using CadBridge.Inspection;
using CadBridge.Metadata;

namespace CadBridge.Plugin;

/// <summary>
/// Read-only inspection view over one bound AutoCAD document and one transaction. The caller must
/// construct and consume this object on AutoCAD's command context. No document name or filesystem
/// path is used as an identifier.
/// </summary>
public sealed class AutoCadInspectionDocument :
    IBoundInspectionDocument,
    IBoundedSelectionInspectionDocument,
    IDisposable
{
    private const string ModelSpaceLabel = "model_space";
    private const string BlockDefinitionLabel = "block_definition";
    private const int MaximumBlockDepth = 16;
    private const int MaximumVerticesPerPolyline = 100_000;
    private const int MaximumObservedPolylineVertices = 200_000;

    private readonly Document _document;
    private readonly Database _database;
    private readonly Transaction _transaction;
    private readonly int _maxBlockDepth;
    private int _observedEntityCount;
    private int _observedPolylineVertexCount;
    private bool _disposed;

    public AutoCadInspectionDocument(Document document, int maxBlockDepth)
    {
        ArgumentNullException.ThrowIfNull(document);
        if (maxBlockDepth is < 0 or > MaximumBlockDepth)
        {
            throw new ArgumentOutOfRangeException(
                nameof(maxBlockDepth),
                $"Block nesting depth must be between 0 and {MaximumBlockDepth}.");
        }

        _document = document;
        _database = document.Database
            ?? throw new ArgumentException("The bound document has no database.", nameof(document));
        if (!Guid.TryParse(_database.FingerprintGuid, out var fingerprint) ||
            fingerprint == Guid.Empty)
        {
            throw new InvalidOperationException("The bound database has no stable fingerprint.");
        }

        DocumentId = fingerprint.ToString("D");
        _maxBlockDepth = maxBlockDepth;
        _transaction = _database.TransactionManager.StartTransaction();
    }

    public string DocumentId { get; }

    public string SourceUnitCode => _database.Insunits.ToString();

    public IEnumerable<InspectionEntity> ReadEntities()
        => ReadEntities(CancellationToken.None);

    public IEnumerable<InspectionEntity> ReadEntities(CancellationToken cancellationToken)
    {
        ThrowIfDisposed();
        _observedEntityCount = 0;
        _observedPolylineVertexCount = 0;
        var blockTable = Open<BlockTable>(_database.BlockTableId);
        foreach (ObjectId recordId in blockTable)
        {
            cancellationToken.ThrowIfCancellationRequested();
            var record = Open<BlockTableRecord>(recordId);
            if (!record.IsLayout)
            {
                continue;
            }

            var space = string.Equals(
                record.Name,
                BlockTableRecord.ModelSpace,
                StringComparison.OrdinalIgnoreCase)
                ? ModelSpaceLabel
                : LayoutLabel(record);
            foreach (ObjectId entityId in record)
            {
                cancellationToken.ThrowIfCancellationRequested();
                if (TryOpenEntity(entityId) is { } entity)
                {
                    yield return ReadEntity(
                        entity,
                        space,
                        nestedDepth: 0,
                        blockAncestry: new HashSet<ObjectId>(),
                        ancestorVisible: true,
                        cancellationToken);
                }
            }
        }
    }

    public IEnumerable<InspectionLayer> ReadLayers()
    {
        ThrowIfDisposed();
        var layers = new List<InspectionLayer>();
        var table = Open<LayerTable>(_database.LayerTableId);
        foreach (ObjectId recordId in table)
        {
            var record = Open<LayerTableRecord>(recordId);
            layers.Add(new InspectionLayer(
                record.Name,
                record.IsOff,
                record.IsFrozen,
                record.IsLocked,
                record.Color?.ColorIndex,
                ReadLinetypeName(record.LinetypeObjectId)));
        }

        return layers;
    }

    public IEnumerable<InspectionStyle> ReadStyles()
    {
        ThrowIfDisposed();
        var styles = new List<InspectionStyle>();
        var textStyles = Open<TextStyleTable>(_database.TextStyleTableId);
        foreach (ObjectId recordId in textStyles)
        {
            var record = Open<TextStyleTableRecord>(recordId);
            if (string.IsNullOrWhiteSpace(record.Name))
            {
                // AutoCAD Mechanical can expose unnamed internal symbol-table records when a
                // plain DXF is opened. They cannot be referenced by the public drawing contract.
                continue;
            }

            styles.Add(new InspectionStyle(
                record.Name,
                InspectionStyleKind.Text,
                Annotative: record.Annotative == AnnotativeStates.True,
                Scale: null));
        }

        var dimensionStyles = Open<DimStyleTable>(_database.DimStyleTableId);
        foreach (ObjectId recordId in dimensionStyles)
        {
            var record = Open<DimStyleTableRecord>(recordId);
            if (string.IsNullOrWhiteSpace(record.Name))
            {
                continue;
            }

            styles.Add(new InspectionStyle(
                record.Name,
                InspectionStyleKind.Dimension,
                Annotative: record.Annotative == AnnotativeStates.True,
                Scale: null));
        }

        return styles;
    }

    public IReadOnlyCollection<StableEntityReference> ReadSelection()
        => ReadSelection(int.MaxValue);

    public IReadOnlyCollection<StableEntityReference> ReadSelection(int maxEntities)
    {
        ThrowIfDisposed();
        ArgumentOutOfRangeException.ThrowIfNegativeOrZero(maxEntities);
        var selection = _document.Editor.SelectImplied();
        if (selection.Status != PromptStatus.OK || selection.Value is null)
        {
            return Array.Empty<StableEntityReference>();
        }

        var references = new List<StableEntityReference>(Math.Min(maxEntities, 1024));
        var handles = new HashSet<string>(StringComparer.Ordinal);
        foreach (SelectedObject selected in selection.Value)
        {
            var id = selected.ObjectId;
            if (id.IsNull || id.IsErased || id.Database != _database)
            {
                continue;
            }

            var reference = ToStableReference(id);
            if (!handles.Add(reference.Handle))
            {
                continue;
            }

            if (references.Count == maxEntities)
            {
                throw new InvalidOperationException("The selection exceeds max_entities.");
            }

            references.Add(reference);
        }

        return references;
    }

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }

        _disposed = true;
        try
        {
            _transaction.Abort();
        }
        finally
        {
            _transaction.Dispose();
        }
    }

    private InspectionEntity ReadEntity(
        Entity entity,
        string space,
        int nestedDepth,
        HashSet<ObjectId> blockAncestry,
        bool ancestorVisible,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        ReserveObservedEntities(1);
        var effectiveVisible = ancestorVisible && IsEntityVisible(entity);
        var geometry = ReadGeometry(entity, nestedDepth, blockAncestry, effectiveVisible, cancellationToken);
        return new InspectionEntity(
            ToStableReference(entity.ObjectId),
            entity.GetRXClass().DxfName,
            entity.Layer,
            ReadStyleName(entity),
            entity.Color?.ColorIndex,
            entity.Linetype,
            ReadContent(entity),
            geometry,
            ReadHarnessMetadata(entity),
            effectiveVisible,
            space);
    }

    private InspectionGeometry ReadGeometry(
        Entity entity,
        int nestedDepth,
        HashSet<ObjectId> blockAncestry,
        bool effectiveVisible,
        CancellationToken cancellationToken)
    {
        var bounds = ReadBounds(entity);
        return entity switch
        {
            DBPoint point => new InspectionGeometry(
                InspectionGeometryKind.Point,
                [ToInspectionPoint(point.Position)],
                Bounds: bounds),
            Line line => new InspectionGeometry(
                InspectionGeometryKind.Line,
                [ToInspectionPoint(line.StartPoint), ToInspectionPoint(line.EndPoint)],
                Bounds: bounds),
            Circle circle => new InspectionGeometry(
                InspectionGeometryKind.Circle,
                [ToInspectionPoint(circle.Center)],
                Bounds: bounds,
                Radius: circle.Radius),
            Arc arc => new InspectionGeometry(
                InspectionGeometryKind.Arc,
                [ToInspectionPoint(arc.Center)],
                Bounds: bounds,
                Radius: arc.Radius,
                StartAngleRadians: arc.StartAngle,
                EndAngleRadians: arc.EndAngle),
            Ellipse ellipse => ReadEllipse(ellipse, bounds),
            Polyline polyline => ReadPolyline(polyline, bounds, cancellationToken),
            Polyline2d polyline => ReadPolyline2d(polyline, bounds, cancellationToken),
            MText text => new InspectionGeometry(
                InspectionGeometryKind.Text,
                [ToInspectionPoint(text.Location)],
                Bounds: bounds,
                RotationRadians: text.Rotation,
                TextHeight: text.TextHeight,
                TextStyle: text.TextStyleName),
            DBText text => new InspectionGeometry(
                InspectionGeometryKind.Text,
                [ToInspectionPoint(text.Position)],
                Bounds: bounds,
                RotationRadians: text.Rotation,
                TextHeight: text.Height,
                TextStyle: text.TextStyleName),
            Dimension dimension => ReadDimension(dimension, bounds),
            Hatch hatch => ReadHatch(hatch, bounds),
            BlockReference block => ReadBlockReference(
                block,
                bounds,
                nestedDepth,
                blockAncestry,
                effectiveVisible,
                cancellationToken),
            _ => new InspectionGeometry(
                InspectionGeometryKind.Other,
                Array.Empty<InspectionPoint>(),
                Bounds: bounds),
        };
    }

    private static InspectionGeometry ReadEllipse(Ellipse ellipse, InspectionBounds? bounds)
    {
        var axis = ellipse.MajorAxis;
        var rotation = Math.Atan2(axis.Y, axis.X);
        return new InspectionGeometry(
            InspectionGeometryKind.Ellipse,
            [ToInspectionPoint(ellipse.Center)],
            Bounds: bounds,
            StartAngleRadians: ellipse.StartAngle,
            EndAngleRadians: ellipse.EndAngle,
            MajorAxis: ellipse.MajorRadius,
            MinorAxis: ellipse.MinorRadius,
            RotationRadians: rotation,
            Closed: Math.Abs((ellipse.EndAngle - ellipse.StartAngle) - (Math.PI * 2.0)) < 1.0e-9);
    }

    private InspectionGeometry ReadPolyline(
        Polyline polyline,
        InspectionBounds? bounds,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        var vertexCount = polyline.NumberOfVertices;
        ReservePolylineVertices(vertexCount);
        var points = new InspectionPoint[vertexCount];
        var bulges = new double[vertexCount];
        for (var index = 0; index < vertexCount; index++)
        {
            cancellationToken.ThrowIfCancellationRequested();
            points[index] = ToInspectionPoint(polyline.GetPoint3dAt(index));
            bulges[index] = polyline.GetBulgeAt(index);
        }

        if (points.Length < 2)
        {
            return new InspectionGeometry(
                InspectionGeometryKind.Other,
                points,
                Bounds: bounds);
        }

        return new InspectionGeometry(
            InspectionGeometryKind.Polyline,
            points,
            Bounds: bounds,
            Closed: polyline.Closed,
            Bulges: bulges);
    }

    private InspectionGeometry ReadPolyline2d(
        Polyline2d polyline,
        InspectionBounds? bounds,
        CancellationToken cancellationToken)
    {
        if (polyline.PolyType != Poly2dType.SimplePoly)
        {
            return new InspectionGeometry(
                InspectionGeometryKind.Other,
                Array.Empty<InspectionPoint>(),
                Bounds: bounds);
        }

        var vertexCount = CountPolyline2dVertices(polyline, cancellationToken);
        ReservePolylineVertices(vertexCount);
        var points = new List<InspectionPoint>(vertexCount);
        var bulges = new List<double>(vertexCount);
        foreach (ObjectId vertexId in polyline)
        {
            cancellationToken.ThrowIfCancellationRequested();
            var vertex = Open<Vertex2d>(vertexId);
            // Vertex2d.Position is OCS. The parent conversion is required to expose WCS.
            points.Add(ToInspectionPoint(polyline.VertexPosition(vertex)));
            bulges.Add(vertex.Bulge);
        }

        if (points.Count != vertexCount)
        {
            throw new InvalidOperationException("Legacy polyline vertex enumeration changed during read.");
        }

        if (points.Count < 2)
        {
            return new InspectionGeometry(
                InspectionGeometryKind.Other,
                points,
                Bounds: bounds);
        }

        return new InspectionGeometry(
            InspectionGeometryKind.Polyline,
            points,
            Bounds: bounds,
            Closed: polyline.Closed,
            Bulges: bulges);
    }

    private static InspectionGeometry ReadDimension(
        Dimension dimension,
        InspectionBounds? bounds)
    {
        var points = dimension switch
        {
            RotatedDimension rotated => new[]
            {
                ToInspectionPoint(rotated.XLine1Point),
                ToInspectionPoint(rotated.XLine2Point),
                ToInspectionPoint(rotated.DimLinePoint),
            },
            AlignedDimension aligned => new[]
            {
                ToInspectionPoint(aligned.XLine1Point),
                ToInspectionPoint(aligned.XLine2Point),
                ToInspectionPoint(aligned.DimLinePoint),
            },
            RadialDimension radial => new[]
            {
                ToInspectionPoint(radial.Center),
                ToInspectionPoint(radial.ChordPoint),
            },
            DiametricDimension diametric => new[]
            {
                ToInspectionPoint(diametric.ChordPoint),
                ToInspectionPoint(diametric.FarChordPoint),
            },
            LineAngularDimension2 angular => new[]
            {
                ToInspectionPoint(angular.XLine1Start),
                ToInspectionPoint(angular.XLine1End),
                ToInspectionPoint(angular.XLine2Start),
                ToInspectionPoint(angular.XLine2End),
            },
            Point3AngularDimension angular => new[]
            {
                ToInspectionPoint(angular.CenterPoint),
                ToInspectionPoint(angular.XLine1Point),
                ToInspectionPoint(angular.XLine2Point),
            },
            ArcDimension arc => new[]
            {
                ToInspectionPoint(arc.CenterPoint),
                ToInspectionPoint(arc.XLine1Point),
                ToInspectionPoint(arc.XLine2Point),
            },
            _ => Array.Empty<InspectionPoint>(),
        };
        return new InspectionGeometry(
            InspectionGeometryKind.Dimension,
            points,
            Bounds: bounds,
            DimensionType: dimension.GetType().Name,
            DimensionStyle: dimension.DimensionStyleName,
            Measurement: ReadDimensionMeasurement(dimension),
            TextOverride: dimension.DimensionText,
            MeasuredEntityRefs: null);
    }

    private InspectionGeometry ReadHatch(Hatch hatch, InspectionBounds? bounds)
    {
        StableEntityReference[]? boundaryReferences = null;
        if (hatch.Associative)
        {
            boundaryReferences = hatch.GetAssociatedObjectIds()
                .Cast<ObjectId>()
                .Where(id => !id.IsNull && !id.IsErased && id.Database == _database)
                .Select(ToStableReference)
                .DistinctBy(reference => reference.Handle, StringComparer.Ordinal)
                .ToArray();
        }

        return new InspectionGeometry(
            InspectionGeometryKind.Hatch,
            Array.Empty<InspectionPoint>(),
            Bounds: bounds,
            PatternName: hatch.PatternName,
            ObservedArea: ReadHatchArea(hatch),
            BoundaryEntityRefs: boundaryReferences);
    }

    private InspectionGeometry ReadBlockReference(
        BlockReference block,
        InspectionBounds? bounds,
        int nestedDepth,
        HashSet<ObjectId> blockAncestry,
        bool effectiveVisible,
        CancellationToken cancellationToken)
    {
        var insertion = ToInspectionPoint(block.Position);
        var scales = block.ScaleFactors;
        var definitionId = block.BlockTableRecord;
        var childEntities = new List<InspectionEntity>();
        var childrenBeyondDepth = 0;
        var nestedDepthRead = 0;
        var definition = Open<BlockTableRecord>(definitionId);
        if (definition.IsFromExternalReference || definition.IsFromOverlayReference)
        {
            // Xrefs are external provenance boundaries. Do not traverse or present their contents
            // as ordinary in-document block geometry.
            return new InspectionGeometry(
                InspectionGeometryKind.Other,
                Array.Empty<InspectionPoint>(),
                Bounds: bounds);
        }

        if (nestedDepth >= _maxBlockDepth || !blockAncestry.Add(definitionId))
        {
            var attributeCount = block.AttributeCollection.Count;
            ReserveObservedEntities(attributeCount);
            var definitionCount = CountReadableEntities(
                definition,
                BridgeInspectionService.MaximumWholeDocumentObservationEntities - _observedEntityCount);
            ReserveObservedEntities(definitionCount);
            childrenBeyondDepth = checked(definitionCount + attributeCount);
        }
        else
        {
            try
            {
                // AttributeReference values live on the insertion, not in the block definition.
                // They are semantically significant for title blocks and BOMs and must therefore
                // participate in both the DrawingModel and the canonical revision.
                foreach (ObjectId attributeId in block.AttributeCollection)
                {
                    cancellationToken.ThrowIfCancellationRequested();
                    if (TryOpenEntity(attributeId) is not { } attribute)
                    {
                        continue;
                    }

                    var observation = ReadEntity(
                        attribute,
                        BlockDefinitionLabel,
                        nestedDepth + 1,
                        blockAncestry,
                        effectiveVisible,
                        cancellationToken);
                    childEntities.Add(observation);
                    nestedDepthRead = Math.Max(
                        nestedDepthRead,
                        1 + observation.Geometry.NestedDepthRead);
                    childrenBeyondDepth += observation.Geometry.ChildrenBeyondDepth;
                }

                foreach (ObjectId childId in definition)
                {
                    cancellationToken.ThrowIfCancellationRequested();
                    if (TryOpenEntity(childId) is not { } child)
                    {
                        continue;
                    }

                    var observation = ReadEntity(
                        child,
                        // Child coordinates and bounds intentionally remain definition-local. The
                        // enclosing block carries insertion, rotation, scale and distortion flags.
                        BlockDefinitionLabel,
                        nestedDepth + 1,
                        blockAncestry,
                        effectiveVisible,
                        cancellationToken);
                    childEntities.Add(observation);
                    nestedDepthRead = Math.Max(
                        nestedDepthRead,
                        1 + observation.Geometry.NestedDepthRead);
                    childrenBeyondDepth += observation.Geometry.ChildrenBeyondDepth;
                }
            }
            finally
            {
                blockAncestry.Remove(definitionId);
            }
        }

        return new InspectionGeometry(
            InspectionGeometryKind.BlockReference,
            [insertion],
            Bounds: bounds,
            RotationRadians: block.Rotation,
            BlockName: definition.Name,
            Insertion: insertion,
            ScaleX: scales.X,
            ScaleY: scales.Y,
            NonUniformScale: Math.Abs(scales.X - scales.Y) > 1.0e-12 ||
                Math.Abs(scales.X - scales.Z) > 1.0e-12,
            NestedDepthRead: nestedDepthRead,
            ChildEntities: childEntities,
            ChildrenBeyondDepth: childrenBeyondDepth);
    }

    private CadHarnessMetadata? ReadHarnessMetadata(Entity entity)
    {
        using var data = entity.GetXDataForApplication(CadHarnessMetadataRegistry.ApplicationName);
        if (data is null)
        {
            return null;
        }

        var values = data.AsArray();
        if (values.Length != 5 ||
            values[0].TypeCode != (int)DxfCode.ExtendedDataRegAppName ||
            values[1].TypeCode != (int)DxfCode.ExtendedDataAsciiString ||
            values[2].TypeCode != (int)DxfCode.ExtendedDataAsciiString ||
            values[3].TypeCode != (int)DxfCode.ExtendedDataAsciiString ||
            values[4].TypeCode != (int)DxfCode.ExtendedDataAsciiString ||
            !string.Equals(
                values[0].Value as string,
                CadHarnessMetadataRegistry.ApplicationName,
                StringComparison.Ordinal) ||
            !string.Equals(values[1].Value as string, "feature_id", StringComparison.Ordinal) ||
            values[2].Value is not string featureId ||
            !string.Equals(values[3].Value as string, "operation_id", StringComparison.Ordinal) ||
            values[4].Value is not string operationId)
        {
            return null;
        }

        try
        {
            return CadHarnessMetadata.Create(featureId, operationId);
        }
        catch (ArgumentException)
        {
            return null;
        }
    }

    private TRecord Open<TRecord>(ObjectId objectId) where TRecord : DBObject =>
        (TRecord)_transaction.GetObject(objectId, OpenMode.ForRead);

    private bool IsEntityVisible(Entity entity)
    {
        if (!entity.Visible || entity.LayerId.IsNull || entity.LayerId.IsErased ||
            entity.LayerId.Database != _database)
        {
            return false;
        }

        var layer = Open<LayerTableRecord>(entity.LayerId);
        return !layer.IsOff && !layer.IsFrozen;
    }

    private Entity? TryOpenEntity(ObjectId objectId)
    {
        if (objectId.IsNull || objectId.IsErased || objectId.Database != _database)
        {
            return null;
        }

        return _transaction.GetObject(objectId, OpenMode.ForRead) as Entity;
    }

    private string? ReadLinetypeName(ObjectId objectId)
    {
        if (objectId.IsNull || objectId.IsErased || objectId.Database != _database)
        {
            return null;
        }

        return (_transaction.GetObject(objectId, OpenMode.ForRead) as LinetypeTableRecord)?.Name;
    }

    private string LayoutLabel(BlockTableRecord record)
    {
        if (record.LayoutId.IsNull || record.LayoutId.IsErased)
        {
            return "layout:unknown";
        }

        var layout = Open<Layout>(record.LayoutId);
        return $"layout:{layout.LayoutName}";
    }

    private static string? ReadStyleName(Entity entity) => entity switch
    {
        Dimension dimension => dimension.DimensionStyleName,
        MText text => text.TextStyleName,
        DBText text => text.TextStyleName,
        _ => null,
    };

    private static string? ReadContent(Entity entity) => entity switch
    {
        MText text => text.Contents,
        DBText text => text.TextString,
        Dimension dimension => dimension.DimensionText,
        _ => null,
    };

    private static double? ReadHatchArea(Hatch hatch)
    {
        try
        {
            var area = hatch.Area;
            return double.IsFinite(area) && area >= 0.0 ? area : null;
        }
        catch (Autodesk.AutoCAD.Runtime.Exception)
        {
            return null;
        }
    }

    private static double? ReadDimensionMeasurement(Dimension dimension)
    {
        try
        {
            var measurement = dimension.Measurement;
            return double.IsFinite(measurement) ? measurement : null;
        }
        catch (Autodesk.AutoCAD.Runtime.Exception)
        {
            // Some imported DXF dimensions require a write-open lazy recomputation before
            // Measurement is available. Read-only inspection must never trigger that mutation.
            return null;
        }
    }

    private static InspectionBounds? ReadBounds(Entity entity)
    {
        try
        {
            var extents = entity.GeometricExtents;
            return new InspectionBounds(
                ToInspectionPoint(extents.MinPoint),
                ToInspectionPoint(extents.MaxPoint));
        }
        catch (Autodesk.AutoCAD.Runtime.Exception)
        {
            return null;
        }
    }

    private static int CountReadableEntities(BlockTableRecord record, int maximumCount)
    {
        var count = 0;
        foreach (ObjectId id in record)
        {
            if (id.IsNull || id.IsErased)
            {
                continue;
            }

            count = checked(count + 1);
            if (count > maximumCount)
            {
                return count;
            }
        }

        return count;
    }

    private void ReserveObservedEntities(int count)
    {
        if (count < 0 || count > BridgeInspectionService.MaximumWholeDocumentObservationEntities - _observedEntityCount)
        {
            throw new InvalidOperationException(
                $"The document exceeds the hard observation limit of {BridgeInspectionService.MaximumWholeDocumentObservationEntities} entities.");
        }

        _observedEntityCount += count;
    }

    private int CountPolyline2dVertices(Polyline2d polyline, CancellationToken cancellationToken)
    {
        var remainingTotal = MaximumObservedPolylineVertices - _observedPolylineVertexCount;
        var maximumAllowed = Math.Min(MaximumVerticesPerPolyline, remainingTotal);
        var count = 0;
        foreach (ObjectId _ in polyline)
        {
            cancellationToken.ThrowIfCancellationRequested();
            if (count == maximumAllowed)
            {
                throw new InvalidOperationException(
                    $"Polyline vertices exceed the hard limit of {MaximumVerticesPerPolyline} per entity or {MaximumObservedPolylineVertices} per inspection.");
            }

            count = checked(count + 1);
        }

        return count;
    }

    private void ReservePolylineVertices(int count)
    {
        if (count < 0 || count > MaximumVerticesPerPolyline ||
            count > MaximumObservedPolylineVertices - _observedPolylineVertexCount)
        {
            throw new InvalidOperationException(
                $"Polyline vertices exceed the hard limit of {MaximumVerticesPerPolyline} per entity or {MaximumObservedPolylineVertices} per inspection.");
        }

        _observedPolylineVertexCount += count;
    }

    private static StableEntityReference ToStableReference(ObjectId objectId) =>
        new(objectId.Handle.ToString());

    private static InspectionPoint ToInspectionPoint(Point3d point) =>
        new(point.X, point.Y, point.Z);

    private void ThrowIfDisposed()
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
    }
}
