using System.Globalization;
using System.Text.Json;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.Geometry;
using CadBridge.Execution;
using CadBridge.Metadata;

namespace CadBridge.Plugin;

/// <summary>Closed, typed representation of one already router-validated plan operation.</summary>
public sealed record AutoCadPlanOperation(
    string OperationId,
    string FeatureId,
    string Type,
    string Layer,
    JsonElement Geometry,
    JsonElement Expected,
    string? TargetEntityRef) : IAtomicJobOperation;

/// <summary>Observed database entity created or touched by one operation.</summary>
public sealed record AutoCadOperationEntity(
    string OperationId,
    string FeatureId,
    ObjectId ObjectId,
    bool Deleted = false);

/// <summary>
/// Explicit AutoCAD database dispatcher. It accepts only the fixed operation vocabulary and fixed
/// geometry field names; there is no command string, reflection, dynamic access, or arbitrary CLR
/// property assignment surface.
/// </summary>
public sealed class AutoCadOperationDispatcher
{
    private sealed record GeometrySchema(
        string Type,
        IReadOnlySet<string> Required,
        IReadOnlySet<string> Optional);

    private static readonly IReadOnlyList<GeometrySchema> GeometrySchemas =
    [
        new GeometrySchema("create_line", Set("start_mm", "end_mm"), Set()),
        new GeometrySchema("create_closed_polyline", Set("vertices_mm"), Set()),
        new GeometrySchema("create_circle", Set("center_mm", "diameter_mm"), Set()),
        new GeometrySchema("create_circle", Set("center_mm", "radius_mm"), Set()),
        new GeometrySchema("create_circles", Set("centers_mm", "diameter_mm"), Set()),
        new GeometrySchema(
            "create_arc",
            Set("center_mm", "radius_mm", "start_angle_deg", "end_angle_deg"),
            Set()),
        new GeometrySchema(
            "create_text",
            Set(
                "position_mm",
                "text",
                "textstyle",
                "text_height_mm",
                "text_bbox_mm",
                "annotation_kind"),
            Set(
                "diameter_mm",
                "count",
                "symbol",
                "field_name",
                "source",
                "source_version",
                "datum_identifier",
                "frame_id",
                "datum_references",
                "certifies_tolerance_chain")),
        new GeometrySchema("create_centerline", Set("start_mm", "end_mm"), Set()),
        new GeometrySchema("create_centermark", Set("center_mm"), Set()),
        new GeometrySchema(
            "create_linear_dimension",
            Set(
                "start_mm",
                "end_mm",
                "text_position_mm",
                "measurement_mm",
                "text_value",
                "dimstyle",
                "textstyle",
                "text_height_mm",
                "text_bbox_mm",
                "annotation_kind"),
            Set()),
        new GeometrySchema(
            "create_diameter_dimension",
            Set(
                "center_mm",
                "text_position_mm",
                "measurement_mm",
                "text_value",
                "dimstyle",
                "textstyle",
                "text_height_mm",
                "text_bbox_mm",
                "annotation_kind"),
            Set()),
        new GeometrySchema("update_entity", Set("properties"), Set()),
        new GeometrySchema("delete_entity", Set(), Set()),
    ];

    public static readonly IReadOnlyList<string> SupportedOperationTypes = GeometrySchemas
        .Select(schema => schema.Type)
        .Distinct(StringComparer.Ordinal)
        .ToArray();

    private readonly MetadataAttachmentService _metadata =
        new(new AutoCadXDataMetadataWriter());
    private readonly List<AutoCadOperationEntity> _entities = [];

    public IReadOnlyList<AutoCadOperationEntity> Entities => _entities;

    public async ValueTask DispatchAsync(
        IAtomicJobOperation operation,
        IAtomicTransaction transaction,
        CancellationToken cancellationToken)
    {
        if (operation is not AutoCadPlanOperation planOperation ||
            transaction is not AutoCadAtomicTransaction autoCadTransaction)
        {
            throw new InvalidOperationException("The AutoCAD dispatcher received an incompatible operation or transaction.");
        }

        cancellationToken.ThrowIfCancellationRequested();
        ValidateGeometry(
            planOperation.Type,
            planOperation.Geometry,
            planOperation.TargetEntityRef);
        switch (planOperation.Type)
        {
            case "create_line":
            case "create_centerline":
                await AppendAsync(
                    new Line(
                        Point(planOperation.Geometry, "start_mm"),
                        Point(planOperation.Geometry, "end_mm")),
                    planOperation,
                    autoCadTransaction,
                    cancellationToken);
                break;
            case "create_closed_polyline":
                await AppendAsync(
                    Polyline(planOperation.Geometry, closed: true),
                    planOperation,
                    autoCadTransaction,
                    cancellationToken);
                break;
            case "create_circle":
                await AppendCircleAsync(
                    Point(planOperation.Geometry, "center_mm"),
                    CircleRadius(planOperation.Geometry),
                    planOperation,
                    autoCadTransaction,
                    cancellationToken);
                break;
            case "create_circles":
                foreach (var center in Points(planOperation.Geometry, "centers_mm"))
                {
                    cancellationToken.ThrowIfCancellationRequested();
                    await AppendCircleAsync(
                        center,
                        PositiveNumber(planOperation.Geometry, "diameter_mm") / 2.0,
                        planOperation,
                        autoCadTransaction,
                        cancellationToken);
                }

                break;
            case "create_arc":
                await AppendAsync(
                    new Arc(
                        Point(planOperation.Geometry, "center_mm"),
                        PositiveNumber(planOperation.Geometry, "radius_mm"),
                        Degrees(planOperation.Geometry, "start_angle_deg"),
                        Degrees(planOperation.Geometry, "end_angle_deg")),
                    planOperation,
                    autoCadTransaction,
                    cancellationToken);
                break;
            case "create_text":
                await AppendAsync(
                    Text(planOperation.Geometry),
                    planOperation,
                    autoCadTransaction,
                    cancellationToken);
                break;
            case "create_centermark":
                await AppendAsync(
                    new DBPoint(Point(planOperation.Geometry, "center_mm")),
                    planOperation,
                    autoCadTransaction,
                    cancellationToken);
                break;
            case "create_linear_dimension":
                await AppendAsync(
                    LinearDimension(planOperation.Geometry, autoCadTransaction.Database.Dimstyle),
                    planOperation,
                    autoCadTransaction,
                    cancellationToken);
                break;
            case "create_diameter_dimension":
                await AppendAsync(
                    DiameterDimension(planOperation.Geometry, autoCadTransaction.Database.Dimstyle),
                    planOperation,
                    autoCadTransaction,
                    cancellationToken);
                break;
            case "update_entity":
                await UpdateEntityAsync(planOperation, autoCadTransaction, cancellationToken);
                break;
            case "delete_entity":
                DeleteEntity(planOperation, autoCadTransaction);
                break;
            default:
                throw new InvalidOperationException("The plan operation type is not supported by this bridge build.");
        }
    }

    public ValueTask ValidateBeforeCommitAsync(
        IAtomicTransaction transaction,
        CancellationToken cancellationToken)
    {
        if (transaction is not AutoCadAtomicTransaction autoCadTransaction)
        {
            throw new InvalidOperationException("Pre-commit validation requires the active AutoCAD transaction.");
        }

        foreach (var result in _entities)
        {
            cancellationToken.ThrowIfCancellationRequested();
            var entity = autoCadTransaction.Transaction.GetObject(
                result.ObjectId,
                OpenMode.ForRead,
                openErased: result.Deleted);
            if (result.Deleted != entity.IsErased)
            {
                throw new InvalidOperationException("The operation did not reach its expected database state.");
            }
        }

        return ValueTask.CompletedTask;
    }

    public static IReadOnlyList<AutoCadPlanOperation> ParseOperations(JsonElement plan)
    {
        var operations = plan.GetProperty("operations");
        var parsed = new List<AutoCadPlanOperation>(operations.GetArrayLength());
        foreach (var operation in operations.EnumerateArray())
        {
            var type = RequiredString(operation, "type");
            if (!SupportedOperationTypes.Contains(type, StringComparer.Ordinal))
            {
                throw new InvalidOperationException("The plan includes an unsupported operation type.");
            }

            var geometry = operation.GetProperty("geometry");
            var targetEntityRef = OptionalString(operation, "target_entity_ref");
            ValidateGeometry(type, geometry, targetEntityRef);
            parsed.Add(new AutoCadPlanOperation(
                RequiredString(operation, "operation_id"),
                RequiredString(operation, "feature_id"),
                type,
                RequiredString(operation, "layer"),
                geometry.Clone(),
                operation.GetProperty("expected").Clone(),
                targetEntityRef));
        }

        return parsed;
    }

    private static void ValidateGeometry(
        string type,
        JsonElement geometry,
        string? targetEntityRef)
    {
        if (geometry.ValueKind != JsonValueKind.Object)
        {
            throw new InvalidOperationException("Operation geometry must be a JSON object.");
        }

        var propertyNames = geometry.EnumerateObject().Select(property => property.Name).ToArray();
        var uniqueNames = propertyNames.ToHashSet(StringComparer.Ordinal);
        if (propertyNames.Length != uniqueNames.Count)
        {
            throw new InvalidOperationException("Operation geometry contains duplicate fields.");
        }

        var matchingSchema = GeometrySchemas
            .Where(schema => string.Equals(schema.Type, type, StringComparison.Ordinal))
            .Any(schema =>
                schema.Required.IsSubsetOf(uniqueNames) &&
                uniqueNames.All(name => schema.Required.Contains(name) || schema.Optional.Contains(name)));
        if (!matchingSchema)
        {
            throw new InvalidOperationException("Operation geometry does not match its closed schema.");
        }

        var mutatesExistingEntity = type is "update_entity" or "delete_entity";
        if (mutatesExistingEntity != (targetEntityRef is not null))
        {
            throw new InvalidOperationException(
                "Only update and delete operations may declare a target entity reference.");
        }

        if (targetEntityRef is not null)
        {
            ValidateTargetReferenceSyntax(targetEntityRef);
        }

        switch (type)
        {
            case "create_line":
            case "create_centerline":
                _ = Point(geometry, "start_mm");
                _ = Point(geometry, "end_mm");
                break;
            case "create_closed_polyline":
                ValidatePointCollection(geometry, "vertices_mm", minimumCount: 3);
                break;
            case "create_circle":
                _ = Point(geometry, "center_mm");
                _ = CircleRadius(geometry);
                break;
            case "create_circles":
                ValidatePointCollection(geometry, "centers_mm", minimumCount: 1);
                _ = PositiveNumber(geometry, "diameter_mm");
                break;
            case "create_arc":
                _ = Point(geometry, "center_mm");
                _ = PositiveNumber(geometry, "radius_mm");
                _ = Degrees(geometry, "start_angle_deg");
                _ = Degrees(geometry, "end_angle_deg");
                break;
            case "create_text":
                ValidateTextGeometry(geometry);
                break;
            case "create_centermark":
                _ = Point(geometry, "center_mm");
                break;
            case "create_linear_dimension":
                _ = Point(geometry, "start_mm");
                _ = Point(geometry, "end_mm");
                ValidateDimensionAnnotation(geometry);
                break;
            case "create_diameter_dimension":
                _ = Point(geometry, "center_mm");
                ValidateDimensionAnnotation(geometry);
                break;
            case "update_entity":
                ValidateUpdateGeometry(geometry);
                break;
            case "delete_entity":
                break;
            default:
                throw new InvalidOperationException("The plan includes an unsupported operation type.");
        }
    }

    private static void ValidateTextGeometry(JsonElement geometry)
    {
        _ = Point(geometry, "position_mm");
        _ = RequiredString(geometry, "text");
        _ = RequiredString(geometry, "textstyle");
        _ = PositiveNumber(geometry, "text_height_mm");
        ValidateNumberArray(geometry, "text_bbox_mm", 4);
        var annotationKind = RequiredString(geometry, "annotation_kind");

        var baseFields = Set(
            "position_mm",
            "text",
            "textstyle",
            "text_height_mm",
            "text_bbox_mm",
            "annotation_kind");
        IReadOnlySet<string> extraFields = annotationKind switch
        {
            "hole_callout" => Set("diameter_mm", "count"),
            "hole_table_row" => Set("symbol", "count", "diameter_mm"),
            "title_block_field" => Set("field_name", "source", "source_version"),
            "gdt_datum_symbol" => Set("datum_identifier"),
            "gdt_feature_control_frame" =>
                Set("frame_id", "datum_references", "certifies_tolerance_chain"),
            _ => throw new InvalidOperationException("The text annotation kind is not supported."),
        };
        var actualFields = geometry.EnumerateObject()
            .Select(property => property.Name)
            .ToHashSet(StringComparer.Ordinal);
        if (!actualFields.SetEquals(baseFields.Concat(extraFields)))
        {
            throw new InvalidOperationException("Text annotation metadata does not match its kind.");
        }

        switch (annotationKind)
        {
            case "hole_callout":
                _ = PositiveNumber(geometry, "diameter_mm");
                _ = RequiredInteger(geometry, "count", 1, int.MaxValue);
                break;
            case "hole_table_row":
                _ = RequiredString(geometry, "symbol");
                _ = RequiredInteger(geometry, "count", 1, int.MaxValue);
                _ = PositiveNumber(geometry, "diameter_mm");
                break;
            case "title_block_field":
                _ = RequiredString(geometry, "field_name");
                _ = RequiredString(geometry, "source");
                _ = RequiredString(geometry, "source_version");
                break;
            case "gdt_datum_symbol":
                _ = RequiredString(geometry, "datum_identifier");
                break;
            case "gdt_feature_control_frame":
                _ = RequiredString(geometry, "frame_id");
                ValidateStringArray(geometry, "datum_references");
                _ = RequiredBoolean(geometry, "certifies_tolerance_chain");
                break;
        }
    }

    private static void ValidateDimensionAnnotation(JsonElement geometry)
    {
        _ = Point(geometry, "text_position_mm");
        _ = PositiveNumber(geometry, "measurement_mm");
        _ = RequiredString(geometry, "text_value");
        _ = RequiredString(geometry, "dimstyle");
        _ = RequiredString(geometry, "textstyle");
        _ = PositiveNumber(geometry, "text_height_mm");
        ValidateNumberArray(geometry, "text_bbox_mm", 4);
        _ = RequiredString(geometry, "annotation_kind");
    }

    private static void ValidateUpdateGeometry(JsonElement geometry)
    {
        var properties = geometry.GetProperty("properties");
        if (properties.ValueKind != JsonValueKind.Object)
        {
            throw new InvalidOperationException("Update properties must be a JSON object.");
        }

        var updates = properties.EnumerateObject().ToArray();
        if (updates.Length > 1)
        {
            throw new InvalidOperationException("A remediation update may change only one property.");
        }

        if (updates.Length == 0)
        {
            return;
        }

        var update = updates[0];
        switch (update.Name)
        {
            case "StyleName":
                _ = RequiredStringValue(update.Value, update.Name);
                break;
            case "TextOverride":
                _ = RequiredStringValue(update.Value, update.Name, allowEmpty: true);
                break;
            case "StartPoint":
            case "EndPoint":
                _ = Point(update.Value);
                break;
            default:
                throw new InvalidOperationException("The requested entity update is not in the bridge allowlist.");
        }
    }

    private async ValueTask AppendCircleAsync(
        Point3d center,
        double radius,
        AutoCadPlanOperation operation,
        AutoCadAtomicTransaction transaction,
        CancellationToken cancellationToken) =>
        await AppendAsync(
            new Circle(center, Vector3d.ZAxis, radius),
            operation,
            transaction,
            cancellationToken);

    private async ValueTask AppendAsync(
        Entity entity,
        AutoCadPlanOperation operation,
        AutoCadAtomicTransaction transaction,
        CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(entity);
        cancellationToken.ThrowIfCancellationRequested();
        entity.SetDatabaseDefaults(transaction.Database);
        ApplyDeclaredStyle(entity, operation.Geometry, transaction);
        entity.Layer = operation.Layer;
        var currentSpace = (BlockTableRecord)transaction.Transaction.GetObject(
            transaction.Database.CurrentSpaceId,
            OpenMode.ForWrite);
        var objectId = currentSpace.AppendEntity(entity);
        transaction.Transaction.AddNewlyCreatedDBObject(entity, true);
        await _metadata.AttachImmediatelyAfterCreationAsync(
            transaction,
            new AutoCadMetadataEntityReference(objectId),
            operation.FeatureId,
            operation.OperationId,
            cancellationToken);
        _entities.Add(new AutoCadOperationEntity(
            operation.OperationId,
            operation.FeatureId,
            objectId));
    }

    private async ValueTask UpdateEntityAsync(
        AutoCadPlanOperation operation,
        AutoCadAtomicTransaction transaction,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        var objectId = ResolveTarget(operation, transaction.Database);
        var entity = (Entity)transaction.Transaction.GetObject(objectId, OpenMode.ForWrite);
        entity.Layer = operation.Layer;
        var properties = operation.Geometry.GetProperty("properties");
        foreach (var property in properties.EnumerateObject())
        {
            switch (property.Name)
            {
                case "StyleName" when entity is DBText text:
                    text.TextStyleId = SymbolId<TextStyleTable>(
                        transaction,
                        transaction.Database.TextStyleTableId,
                        RequiredStringValue(property.Value, property.Name));
                    break;
                case "StyleName" when entity is MText text:
                    text.TextStyleId = SymbolId<TextStyleTable>(
                        transaction,
                        transaction.Database.TextStyleTableId,
                        RequiredStringValue(property.Value, property.Name));
                    break;
                case "StyleName" when entity is Dimension dimension:
                    dimension.DimensionStyle = SymbolId<DimStyleTable>(
                        transaction,
                        transaction.Database.DimStyleTableId,
                        RequiredStringValue(property.Value, property.Name));
                    break;
                case "TextOverride" when entity is Dimension dimension:
                    dimension.DimensionText = RequiredStringValue(property.Value, property.Name, allowEmpty: true);
                    break;
                case "StartPoint" when entity is Line line:
                    line.StartPoint = Point(property.Value);
                    break;
                case "EndPoint" when entity is Line line:
                    line.EndPoint = Point(property.Value);
                    break;
                default:
                    throw new InvalidOperationException("The requested entity update is not in the bridge allowlist.");
            }
        }

        var reference = new AutoCadMetadataEntityReference(objectId);
        await _metadata.AttachImmediatelyAfterCreationAsync(
            transaction,
            reference,
            operation.FeatureId,
            operation.OperationId,
            cancellationToken);
        var persisted = await _metadata.ReadAsync(transaction, reference, cancellationToken);
        if (persisted is null ||
            !string.Equals(persisted.FeatureId, operation.FeatureId, StringComparison.Ordinal) ||
            !string.Equals(persisted.OperationId, operation.OperationId, StringComparison.Ordinal))
        {
            throw new InvalidOperationException(
                "Updated entity CADHARNESS metadata did not round-trip in the active transaction.");
        }

        _entities.Add(new AutoCadOperationEntity(
            operation.OperationId,
            operation.FeatureId,
            objectId));
    }

    private void DeleteEntity(
        AutoCadPlanOperation operation,
        AutoCadAtomicTransaction transaction)
    {
        var objectId = ResolveTarget(operation, transaction.Database);
        var entity = (Entity)transaction.Transaction.GetObject(objectId, OpenMode.ForWrite);
        entity.Erase();
        _entities.Add(new AutoCadOperationEntity(
            operation.OperationId,
            operation.FeatureId,
            objectId,
            Deleted: true));
    }

    private static Polyline Polyline(JsonElement geometry, bool closed)
    {
        var vertices = geometry.GetProperty("vertices_mm");
        if (vertices.ValueKind != JsonValueKind.Array || vertices.GetArrayLength() < 3)
        {
            throw new InvalidOperationException("A closed polyline requires at least three vertices.");
        }

        var polyline = new Polyline(vertices.GetArrayLength()) { Closed = closed };
        var index = 0;
        foreach (var vertex in vertices.EnumerateArray())
        {
            var point = Point(vertex);
            polyline.AddVertexAt(index++, new Point2d(point.X, point.Y), 0.0, 0.0, 0.0);
        }

        return polyline;
    }

    private static DBText Text(JsonElement geometry) =>
        new()
        {
            TextString = RequiredString(geometry, "text"),
            Position = Point(geometry, "position_mm"),
            Height = PositiveNumber(geometry, "text_height_mm"),
        };

    private static RotatedDimension LinearDimension(JsonElement geometry, ObjectId dimensionStyle)
    {
        var start = Point(geometry, "start_mm");
        var end = Point(geometry, "end_mm");
        var rotation = Math.Atan2(end.Y - start.Y, end.X - start.X);
        return new RotatedDimension(
            rotation,
            start,
            end,
            Point(geometry, "text_position_mm"),
            RequiredString(geometry, "text_value"),
            dimensionStyle);
    }

    private static DiametricDimension DiameterDimension(JsonElement geometry, ObjectId dimensionStyle)
    {
        var center = Point(geometry, "center_mm");
        var textPosition = Point(geometry, "text_position_mm");
        var radius = PositiveNumber(geometry, "measurement_mm") / 2.0;
        var radial = textPosition - center;
        var direction = radial.Length > 1e-9 ? radial.GetNormal() : Vector3d.XAxis;
        var chord = center + (direction * radius);
        var farChord = center - (direction * radius);
        var result = new DiametricDimension(
            chord,
            farChord,
            Math.Max(0.0, textPosition.DistanceTo(chord)),
            RequiredString(geometry, "text_value"),
            dimensionStyle)
        {
            TextPosition = textPosition,
            UsingDefaultTextPosition = false,
        };

        return result;
    }

    private static ObjectId ResolveTarget(AutoCadPlanOperation operation, Database database)
    {
        if (operation.TargetEntityRef is null)
        {
            throw new InvalidOperationException("A mutating operation requires a target entity reference.");
        }

        return ResolveReference(operation.TargetEntityRef, database);
    }

    private static ObjectId ResolveReference(string reference, Database database)
    {
        const string Prefix = "acad:handle:";
        if (!reference.StartsWith(Prefix, StringComparison.Ordinal))
        {
            throw new InvalidOperationException(
                "The target entity reference must use the fixed acad:handle: namespace.");
        }

        var value = reference[Prefix.Length..];
        if (value.Length is < 1 or > 16 || !value.All(Uri.IsHexDigit) ||
            !long.TryParse(value, NumberStyles.HexNumber, CultureInfo.InvariantCulture, out var handleValue) ||
            !database.TryGetObjectId(new Handle(handleValue), out var objectId) || objectId.IsErased)
        {
            throw new InvalidOperationException("The target entity reference is not present in the bound document.");
        }

        return objectId;
    }

    private static ObjectId SymbolId<TTable>(
        AutoCadAtomicTransaction transaction,
        ObjectId tableId,
        string name)
        where TTable : SymbolTable
    {
        var table = (TTable)transaction.Transaction.GetObject(tableId, OpenMode.ForRead);
        if (!table.Has(name))
        {
            throw new InvalidOperationException("The requested drawing style is not present in the bound document.");
        }

        return table[name];
    }

    private static HashSet<string> Set(params string[] values) =>
        new(values, StringComparer.Ordinal);

    private static double CircleRadius(JsonElement geometry) =>
        geometry.TryGetProperty("radius_mm", out _)
            ? PositiveNumber(geometry, "radius_mm")
            : PositiveNumber(geometry, "diameter_mm") / 2.0;

    private static void ApplyDeclaredStyle(
        Entity entity,
        JsonElement geometry,
        AutoCadAtomicTransaction transaction)
    {
        if (entity is DBText text && geometry.TryGetProperty("textstyle", out var textStyle))
        {
            text.TextStyleId = SymbolId<TextStyleTable>(
                transaction,
                transaction.Database.TextStyleTableId,
                RequiredStringValue(textStyle, "textstyle"));
        }
        else if (entity is Dimension dimension && geometry.TryGetProperty("dimstyle", out var dimStyle))
        {
            dimension.DimensionStyle = SymbolId<DimStyleTable>(
                transaction,
                transaction.Database.DimStyleTableId,
                RequiredStringValue(dimStyle, "dimstyle"));
        }
    }

    private static void ValidateTargetReferenceSyntax(string reference)
    {
        const string Prefix = "acad:handle:";
        var value = reference.StartsWith(Prefix, StringComparison.Ordinal)
            ? reference[Prefix.Length..]
            : string.Empty;
        if (value.Length is < 1 or > 16 || !value.All(Uri.IsHexDigit))
        {
            throw new InvalidOperationException(
                "The target entity reference must use the fixed acad:handle: namespace.");
        }
    }

    private static void ValidatePointCollection(
        JsonElement owner,
        string propertyName,
        int minimumCount)
    {
        var values = owner.GetProperty(propertyName);
        if (values.ValueKind != JsonValueKind.Array || values.GetArrayLength() < minimumCount)
        {
            throw new InvalidOperationException("A geometry point collection is too small.");
        }

        foreach (var value in values.EnumerateArray())
        {
            _ = Point(value);
        }
    }

    private static void ValidateNumberArray(JsonElement owner, string propertyName, int count)
    {
        var values = owner.GetProperty(propertyName);
        if (values.ValueKind != JsonValueKind.Array || values.GetArrayLength() != count)
        {
            throw new InvalidOperationException("A geometry number array has an invalid length.");
        }

        foreach (var value in values.EnumerateArray())
        {
            _ = FiniteNumber(value);
        }
    }

    private static void ValidateStringArray(JsonElement owner, string propertyName)
    {
        var values = owner.GetProperty(propertyName);
        if (values.ValueKind != JsonValueKind.Array)
        {
            throw new InvalidOperationException("An operation string collection must be an array.");
        }

        foreach (var value in values.EnumerateArray())
        {
            _ = RequiredStringValue(value, propertyName);
        }
    }

    private static Point3d Point(JsonElement owner, string propertyName) =>
        Point(owner.GetProperty(propertyName));

    private static Point3d Point(JsonElement value)
    {
        if (value.ValueKind != JsonValueKind.Array || value.GetArrayLength() is < 2 or > 3)
        {
            throw new InvalidOperationException("A geometry point must contain two or three coordinates.");
        }

        var coordinates = value.EnumerateArray().Select(FiniteNumber).ToArray();
        return new Point3d(coordinates[0], coordinates[1], coordinates.Length == 3 ? coordinates[2] : 0.0);
    }

    private static IReadOnlyList<Point3d> Points(JsonElement owner, string propertyName)
    {
        var values = owner.GetProperty(propertyName);
        if (values.ValueKind != JsonValueKind.Array || values.GetArrayLength() == 0)
        {
            throw new InvalidOperationException("A geometry point collection cannot be empty.");
        }

        return values.EnumerateArray().Select(Point).ToArray();
    }

    private static double PositiveNumber(JsonElement owner, string propertyName)
    {
        var value = FiniteNumber(owner.GetProperty(propertyName));
        return value > 0.0 ? value : throw new InvalidOperationException("A geometry value must be positive.");
    }

    private static double Degrees(JsonElement owner, string propertyName) =>
        FiniteNumber(owner.GetProperty(propertyName)) * Math.PI / 180.0;

    private static double FiniteNumber(JsonElement value)
    {
        if (value.ValueKind != JsonValueKind.Number || !value.TryGetDouble(out var number) ||
            !double.IsFinite(number))
        {
            throw new InvalidOperationException("A geometry coordinate must be a finite number.");
        }

        return number;
    }

    private static int RequiredInteger(JsonElement owner, string propertyName, int minimum, int maximum)
    {
        if (!owner.TryGetProperty(propertyName, out var property) ||
            property.ValueKind != JsonValueKind.Number || !property.TryGetInt32(out var value) ||
            value < minimum || value > maximum)
        {
            throw new InvalidOperationException("An operation integer is outside its allowed range.");
        }

        return value;
    }

    private static bool RequiredBoolean(JsonElement owner, string propertyName)
    {
        var value = owner.GetProperty(propertyName);
        return value.ValueKind switch
        {
            JsonValueKind.True => true,
            JsonValueKind.False => false,
            _ => throw new InvalidOperationException("An operation boolean has an invalid representation."),
        };
    }

    private static string RequiredString(
        JsonElement owner,
        string propertyName,
        bool allowEmpty = false) =>
        RequiredStringValue(owner.GetProperty(propertyName), propertyName, allowEmpty);

    private static string RequiredStringValue(
        JsonElement value,
        string fieldName,
        bool allowEmpty = false)
    {
        if (value.ValueKind != JsonValueKind.String || value.GetString() is not { } text ||
            (!allowEmpty && text.Length == 0) || text.Length > 256 || text.Any(char.IsControl))
        {
            throw new InvalidOperationException($"Operation field {fieldName} is not a bounded string.");
        }

        return text;
    }

    private static string? OptionalString(JsonElement owner, string propertyName)
    {
        var value = owner.GetProperty(propertyName);
        return value.ValueKind == JsonValueKind.Null ? null : RequiredStringValue(value, propertyName);
    }
}
