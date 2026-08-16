using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;
using CadBridge.Hosting;
using CadBridge.Inspection;

namespace CadBridge.Plugin;

/// <summary>
/// Host-observed document metadata that is not part of an inspection snapshot. Values are explicit
/// so conversion cannot infer units, normalization, paths, or document state from geometry.
/// </summary>
public sealed record AutoCadContractContext(
    string DisplayName,
    string PathHash,
    string SourceUnitCode,
    double? ToMillimetresFactor,
    string ActiveSpace = "model",
    string? ActiveLayout = null,
    string? TemplateName = null,
    bool ReadOnly = false,
    double ArcChordToleranceMillimetres = 0.01);

/// <summary>
/// Converts validated read-only AutoCAD observations into the closed Python wire contracts. It
/// never derives missing geometry or extents and never returns an AutoCAD path or transient id.
/// </summary>
public sealed class AutoCadContractConverter
{
    private const string SchemaVersion = "1.13";
    private const string HandlePrefix = "acad:handle:";
    private readonly AutoCadContractContext _context;
    private readonly double _coordinateScale;

    public AutoCadContractConverter(AutoCadContractContext context)
    {
        ArgumentNullException.ThrowIfNull(context);
        ValidateText(context.DisplayName, nameof(context.DisplayName));
        if (context.DisplayName.IndexOfAny(['/', '\\']) >= 0)
        {
            throw new ArgumentException("DisplayName must not contain a filesystem path.", nameof(context));
        }

        ValidatePathHash(context.PathHash);
        ValidateText(context.SourceUnitCode, nameof(context.SourceUnitCode));
        ValidateText(context.ActiveSpace, nameof(context.ActiveSpace));
        ValidateOptionalText(context.ActiveLayout, nameof(context.ActiveLayout));
        ValidateOptionalText(context.TemplateName, nameof(context.TemplateName));
        if (context.ToMillimetresFactor is { } factor &&
            (!double.IsFinite(factor) || factor <= 0.0))
        {
            throw new ArgumentOutOfRangeException(
                nameof(context),
                "The millimetre conversion factor must be finite and positive when known.");
        }

        if (!double.IsFinite(context.ArcChordToleranceMillimetres) ||
            context.ArcChordToleranceMillimetres <= 0.0)
        {
            throw new ArgumentOutOfRangeException(
                nameof(context),
                "The arc chord tolerance must be finite and positive.");
        }

        _context = context;
        _coordinateScale = context.ToMillimetresFactor ?? 1.0;
    }

    public JsonObject ToDocumentSnapshot(
        DocumentInspectionSnapshot snapshot,
        bool includeLayers,
        bool includeStyles,
        CancellationToken cancellationToken = default)
    {
        ValidateSnapshot(snapshot);
        cancellationToken.ThrowIfCancellationRequested();
        return new JsonObject
        {
            ["schema_version"] = SchemaVersion,
            ["document_id"] = snapshot.DocumentId,
            ["revision"] = snapshot.Revision,
            ["path_hash"] = _context.PathHash,
            ["display_name"] = _context.DisplayName,
            ["units"] = SnapshotUnit(_context.SourceUnitCode),
            ["active_space"] = _context.ActiveSpace,
            ["active_layout"] = _context.ActiveLayout,
            ["layers"] = includeLayers
                ? Layers(snapshot.Layers, cancellationToken)
                : new JsonArray(),
            ["dimension_styles"] = includeStyles
                ? Styles(snapshot.Styles, InspectionStyleKind.Dimension, cancellationToken)
                : new JsonArray(),
            ["text_styles"] = includeStyles
                ? Styles(snapshot.Styles, InspectionStyleKind.Text, cancellationToken)
                : new JsonArray(),
            ["entity_count"] = snapshot.Entities.Count,
            ["template_name"] = _context.TemplateName,
            ["read_only"] = _context.ReadOnly,
        };
    }

    public JsonObject ToSelectionSnapshot(
        SelectionInspectionSnapshot snapshot,
        int maxEntities,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(snapshot);
        ValidateText(snapshot.DocumentId, nameof(snapshot.DocumentId));
        ValidateText(snapshot.Revision, nameof(snapshot.Revision));
        ArgumentNullException.ThrowIfNull(snapshot.Entities);
        if (maxEntities <= 0)
        {
            throw new ArgumentOutOfRangeException(nameof(maxEntities));
        }

        var entities = new JsonArray();
        foreach (var entity in snapshot.Entities.Take(maxEntities))
        {
            cancellationToken.ThrowIfCancellationRequested();
            entities.Add(new JsonObject
            {
                ["entity_ref"] = ToWireEntityReference(entity.EntityRef),
                ["entity_type"] = EntityType(entity),
                ["layer"] = entity.Layer,
                ["feature_id"] = entity.HarnessMetadata?.FeatureId,
                // SelectionSnapshot has no unit declaration. Unlabelled source-unit measurements
                // would be ambiguous, so only identity metadata is emitted here.
                ["measurements"] = new JsonObject(),
            });
        }

        return new JsonObject
        {
            ["schema_version"] = SchemaVersion,
            ["document_id"] = snapshot.DocumentId,
            ["revision"] = snapshot.Revision,
            ["entities"] = entities,
            ["truncated"] = snapshot.Entities.Count > maxEntities,
        };
    }

    public JsonObject ToDrawingSummary(
        DocumentInspectionSnapshot snapshot,
        SemanticDrawingHostRequest request,
        CancellationToken cancellationToken = default)
    {
        ValidateSemanticRequest(snapshot, request, SemanticDrawingResponseContract.DrawingSummary);
        var entities = EntitiesInScope(snapshot.Entities, request.Scope).ToArray();
        if (entities.Length > request.MaxEntities)
        {
            throw new InvalidOperationException(
                "The requested scope exceeds max_entities; partial drawing summaries are forbidden.");
        }
        var unsupported = new Dictionary<string, int>(StringComparer.Ordinal);
        foreach (var entity in entities)
        {
            cancellationToken.ThrowIfCancellationRequested();
            if (!CanRepresent(entity))
            {
                Increment(unsupported, EntityType(entity));
            }
        }

        return new JsonObject
        {
            ["schema_version"] = SchemaVersion,
            ["document_id"] = snapshot.DocumentId,
            ["revision"] = snapshot.Revision,
            ["counts_by_entity_type"] = Counts(entities, EntityType),
            ["counts_by_layer"] = Counts(entities, entity => entity.Layer),
            ["counts_by_space"] = Counts(entities, entity => WireSpace(entity.Space)),
            ["unsupported"] = Unsupported(unsupported),
            ["coverage_complete"] = unsupported.Count == 0,
        };
    }

    public JsonObject ToDrawingModel(
        DocumentInspectionSnapshot snapshot,
        SemanticDrawingHostRequest request,
        CancellationToken cancellationToken = default)
    {
        ValidateSemanticRequest(snapshot, request, SemanticDrawingResponseContract.DrawingModel);
        if (request.Scope is null)
        {
            throw new ArgumentException("A drawing model requires an explicit read scope.", nameof(request));
        }

        if (!request.IncludeGeometry)
        {
            throw new ArgumentException(
                "The DrawingModel contract requires geometry; include_geometry must be true.",
                nameof(request));
        }

        var sourceEntities = EntitiesInScope(snapshot.Entities, request.Scope).ToArray();
        if (sourceEntities.Length > request.MaxEntities)
        {
            throw new InvalidOperationException(
                "The requested scope exceeds max_entities; partial drawing models are forbidden.");
        }

        var unsupported = new Dictionary<string, int>(StringComparer.Ordinal);
        var entities = new JsonArray();
        foreach (var entity in sourceEntities)
        {
            cancellationToken.ThrowIfCancellationRequested();
            if (TryEntity(
                    entity,
                    depth: 0,
                    request.MaxBlockNestingDepth,
                    unsupported,
                    cancellationToken,
                    out var record))
            {
                entities.Add(record);
            }
        }

        return new JsonObject
        {
            ["schema_version"] = SchemaVersion,
            ["document_id"] = snapshot.DocumentId,
            ["revision"] = snapshot.Revision,
            ["display_name"] = _context.DisplayName,
            ["source_unit_code"] = _context.SourceUnitCode,
            ["to_mm_factor"] = _context.ToMillimetresFactor,
            ["geometry_normalized"] = _context.ToMillimetresFactor is not null,
            ["scope"] = Scope(request.Scope),
            ["entities"] = entities,
            ["layers"] = Layers(snapshot.Layers, cancellationToken),
            ["dimension_styles"] = Styles(
                snapshot.Styles,
                InspectionStyleKind.Dimension,
                cancellationToken),
            ["text_styles"] = Styles(
                snapshot.Styles,
                InspectionStyleKind.Text,
                cancellationToken),
            ["unsupported"] = Unsupported(unsupported),
            ["coverage_complete"] = unsupported.Count == 0,
            ["arc_chord_tolerance_mm"] = _context.ArcChordToleranceMillimetres,
        };
    }

    public static string ToWireEntityReference(StableEntityReference reference)
    {
        ArgumentNullException.ThrowIfNull(reference);
        return $"{HandlePrefix}{reference.Handle}";
    }

    public static string ComputePathHash(string normalizedPath)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(normalizedPath);
        if (normalizedPath.Any(char.IsControl))
        {
            throw new ArgumentException("A normalized path must not contain control characters.", nameof(normalizedPath));
        }

        var digest = SHA256.HashData(Encoding.UTF8.GetBytes(normalizedPath));
        return $"sha256:{Convert.ToHexString(digest).ToLowerInvariant()}";
    }

    private static void ValidateSemanticRequest(
        DocumentInspectionSnapshot snapshot,
        SemanticDrawingHostRequest request,
        SemanticDrawingResponseContract expectedContract)
    {
        ValidateSnapshot(snapshot);
        ArgumentNullException.ThrowIfNull(request);
        ArgumentNullException.ThrowIfNull(request.Source);
        if (request.ResponseContract != expectedContract)
        {
            throw new ArgumentException("The requested response contract does not match the converter method.", nameof(request));
        }

        if (!string.Equals(request.Source.Kind, "active_document", StringComparison.Ordinal) ||
            !string.Equals(request.Source.Ref, snapshot.DocumentId, StringComparison.Ordinal) ||
            request.Source.Format is not ("dwg" or "dxf") ||
            request.MaxEntities <= 0 ||
            request.MaxBlockNestingDepth is < 1 or > 10)
        {
            throw new ArgumentException("The semantic request does not match the inspected document.", nameof(request));
        }
    }

    private bool TryEntity(
        InspectionEntity entity,
        int depth,
        int maximumDepth,
        Dictionary<string, int> unsupported,
        CancellationToken cancellationToken,
        out JsonObject? record)
    {
        cancellationToken.ThrowIfCancellationRequested();
        record = null;
        if (entity.Geometry.Bounds is null ||
            !TryGeometry(entity, depth, maximumDepth, unsupported, cancellationToken, out var geometry))
        {
            Increment(unsupported, EntityType(entity));
            return false;
        }

        var bounds = entity.Geometry.Bounds;
        record = new JsonObject
        {
            ["entity_ref"] = ToWireEntityReference(entity.EntityRef),
            ["entity_type"] = EntityType(entity),
            ["layer"] = entity.Layer,
            ["visible"] = entity.Visible,
            ["space"] = WireSpace(entity.Space),
            ["geometry"] = geometry,
            ["bounding_box_mm"] = new JsonArray(
                Scale(bounds.Minimum.X),
                Scale(bounds.Minimum.Y),
                Scale(bounds.Maximum.X),
                Scale(bounds.Maximum.Y)),
            ["non_uniform_scale"] = entity.Geometry.NonUniformScale,
            ["feature_id"] = entity.HarnessMetadata?.FeatureId,
        };
        return true;
    }

    private bool TryGeometry(
        InspectionEntity entity,
        int depth,
        int maximumDepth,
        Dictionary<string, int> unsupported,
        CancellationToken cancellationToken,
        out JsonObject? result)
    {
        var geometry = entity.Geometry;
        result = geometry.Kind switch
        {
            InspectionGeometryKind.Point when geometry.Points.Count == 1 => new JsonObject
            {
                ["kind"] = "point",
                ["position_mm"] = Point(geometry.Points[0]),
            },
            InspectionGeometryKind.Line when geometry.Points.Count == 2 => new JsonObject
            {
                ["kind"] = "line",
                ["start_mm"] = Point(geometry.Points[0]),
                ["end_mm"] = Point(geometry.Points[1]),
            },
            InspectionGeometryKind.Circle when geometry.Points.Count == 1 && geometry.Radius is { } radius =>
                new JsonObject
                {
                    ["kind"] = "circle",
                    ["center_mm"] = Point(geometry.Points[0]),
                    ["radius_mm"] = Scale(radius),
                },
            InspectionGeometryKind.Arc when
                geometry.Points.Count == 1 &&
                geometry.Radius is { } radius &&
                geometry.StartAngleRadians is { } start &&
                geometry.EndAngleRadians is { } end => new JsonObject
                {
                    ["kind"] = "arc",
                    ["center_mm"] = Point(geometry.Points[0]),
                    ["radius_mm"] = Scale(radius),
                    ["start_angle_deg"] = RadiansToDegrees(start),
                    ["end_angle_deg"] = RadiansToDegrees(end),
                },
            InspectionGeometryKind.Ellipse when
                geometry.Closed &&
                geometry.Points.Count == 1 &&
                geometry.MajorAxis is { } major &&
                geometry.MinorAxis is { } minor &&
                geometry.RotationRadians is { } rotation => new JsonObject
                {
                    ["kind"] = "ellipse",
                    ["center_mm"] = Point(geometry.Points[0]),
                    ["major_axis_mm"] = Scale(major),
                    ["minor_axis_mm"] = Scale(minor),
                    ["rotation_deg"] = RadiansToDegrees(rotation),
                },
            InspectionGeometryKind.Polyline when geometry.Points.Count >= 2 => Polyline(geometry),
            InspectionGeometryKind.Text when
                geometry.Points.Count == 1 &&
                geometry.TextHeight is { } height &&
                geometry.TextStyle is not null &&
                entity.Content is not null => new JsonObject
                {
                    ["kind"] = "text",
                    ["insertion_mm"] = Point(geometry.Points[0]),
                    ["height_mm"] = Scale(height),
                    ["text_style"] = geometry.TextStyle,
                    ["content"] = entity.Content,
                },
            InspectionGeometryKind.Dimension when
                geometry.DimensionType is not null &&
                geometry.DimensionStyle is not null => Dimension(geometry),
            InspectionGeometryKind.Hatch when geometry.PatternName is not null => Hatch(geometry),
            InspectionGeometryKind.BlockReference when
                geometry.BlockName is not null &&
                geometry.Insertion is not null &&
                geometry.ScaleX is { } &&
                geometry.ScaleY is { } => Block(
                    geometry,
                    depth,
                    maximumDepth,
                    unsupported,
                    cancellationToken),
            _ => null,
        };
        return result is not null;
    }

    private JsonObject Polyline(InspectionGeometry geometry)
    {
        var vertices = new JsonArray();
        for (var index = 0; index < geometry.Points.Count; index++)
        {
            vertices.Add(new JsonObject
            {
                ["point_mm"] = Point(geometry.Points[index]),
                ["bulge"] = geometry.Bulges?[index] ?? 0.0,
            });
        }

        return new JsonObject
        {
            ["kind"] = "polyline",
            ["vertices"] = vertices,
            ["closed"] = geometry.Closed,
        };
    }

    private JsonObject Dimension(InspectionGeometry geometry)
    {
        var angular = geometry.DimensionType?.Contains("Angular", StringComparison.OrdinalIgnoreCase) == true;
        return new JsonObject
        {
            ["kind"] = "dimension",
            ["dimension_type"] = geometry.DimensionType,
            ["dimension_style"] = geometry.DimensionStyle,
            // AutoCAD reports angular dimensions in radians. The Python field is explicitly mm,
            // so an angular value is omitted instead of being mislabeled as a length.
            ["measurement_mm"] = angular || geometry.Measurement is null
                ? null
                : Scale(geometry.Measurement.Value),
            ["text_override"] = geometry.TextOverride,
            ["measured_entity_refs"] = References(geometry.MeasuredEntityRefs),
        };
    }

    private JsonObject Hatch(InspectionGeometry geometry) => new()
    {
        ["kind"] = "hatch",
        ["pattern_name"] = geometry.PatternName,
        ["area_mm2"] = geometry.ObservedArea is { } area
            ? FiniteProduct(area, _coordinateScale, _coordinateScale)
            : null,
        ["boundary_entity_refs"] = References(geometry.BoundaryEntityRefs),
    };

    private JsonObject Block(
        InspectionGeometry geometry,
        int depth,
        int maximumDepth,
        Dictionary<string, int> unsupported,
        CancellationToken cancellationToken)
    {
        var children = new JsonArray();
        var beyondDepth = geometry.ChildrenBeyondDepth;
        var sourceChildren = geometry.ChildEntities ?? Array.Empty<InspectionEntity>();
        if (depth >= maximumDepth)
        {
            foreach (var child in sourceChildren)
            {
                cancellationToken.ThrowIfCancellationRequested();
                beyondDepth = checked(beyondDepth + CountObservedTree(child, cancellationToken));
            }
        }
        else
        {
            foreach (var child in sourceChildren)
            {
                if (TryEntity(
                        child,
                        depth + 1,
                        maximumDepth,
                        unsupported,
                        cancellationToken,
                        out var childRecord))
                {
                    children.Add(childRecord);
                }
            }
        }

        return new JsonObject
        {
            ["kind"] = "block_reference",
            ["block_name"] = geometry.BlockName,
            ["insertion_mm"] = Point(geometry.Insertion!),
            ["scale"] = new JsonArray(geometry.ScaleX, geometry.ScaleY),
            ["rotation_deg"] = RadiansToDegrees(geometry.RotationRadians ?? 0.0),
            ["non_uniform_scale"] = geometry.NonUniformScale,
            ["nested_depth_read"] = Math.Min(geometry.NestedDepthRead, Math.Max(0, maximumDepth - depth)),
            ["child_entities"] = children,
            ["children_beyond_depth"] = beyondDepth,
        };
    }

    private static int CountObservedTree(InspectionEntity entity, CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        var count = checked(1 + entity.Geometry.ChildrenBeyondDepth);
        foreach (var child in entity.Geometry.ChildEntities ?? Array.Empty<InspectionEntity>())
        {
            count = checked(count + CountObservedTree(child, cancellationToken));
        }

        return count;
    }

    private static IEnumerable<InspectionEntity> EntitiesInScope(
        IReadOnlyList<InspectionEntity> entities,
        SemanticReadScopeHostRequest? scope)
    {
        ArgumentNullException.ThrowIfNull(entities);
        if (scope is null)
        {
            return entities;
        }

        return scope.Kind switch
        {
            "model_space" => entities.Where(entity =>
                string.Equals(entity.Space, "model_space", StringComparison.OrdinalIgnoreCase)),
            "layer" => entities.Where(entity =>
                string.Equals(entity.Space, "model_space", StringComparison.OrdinalIgnoreCase) &&
                string.Equals(entity.Layer, scope.LayerName, StringComparison.OrdinalIgnoreCase)),
            "layout" => entities.Where(entity =>
                string.Equals(entity.Space, $"layout:{scope.LayoutName}", StringComparison.OrdinalIgnoreCase)),
            "selection" => entities.Where(entity =>
                scope.EntityRefs.Contains(ToWireEntityReference(entity.EntityRef), StringComparer.Ordinal)),
            _ => throw new ArgumentException("Unsupported semantic read scope.", nameof(scope)),
        };
    }

    private static JsonObject Scope(SemanticReadScopeHostRequest scope) => new()
    {
        ["kind"] = scope.Kind,
        ["layer_name"] = scope.LayerName,
        ["layout_name"] = scope.LayoutName,
        ["entity_refs"] = Strings(scope.EntityRefs),
    };

    private static bool CanRepresent(InspectionEntity entity)
    {
        var geometry = entity.Geometry;
        if (geometry.Bounds is null)
        {
            return false;
        }

        return geometry.Kind switch
        {
            InspectionGeometryKind.Point => geometry.Points.Count == 1,
            InspectionGeometryKind.Line => geometry.Points.Count == 2,
            InspectionGeometryKind.Circle => geometry.Points.Count == 1 && geometry.Radius is not null,
            InspectionGeometryKind.Arc => geometry.Points.Count == 1 &&
                geometry.Radius is not null &&
                geometry.StartAngleRadians is not null &&
                geometry.EndAngleRadians is not null,
            InspectionGeometryKind.Ellipse => geometry.Closed &&
                geometry.Points.Count == 1 &&
                geometry.MajorAxis is not null &&
                geometry.MinorAxis is not null &&
                geometry.RotationRadians is not null,
            InspectionGeometryKind.Polyline => geometry.Points.Count >= 2,
            InspectionGeometryKind.Text => geometry.Points.Count == 1 &&
                geometry.TextHeight is not null &&
                geometry.TextStyle is not null &&
                entity.Content is not null,
            InspectionGeometryKind.Dimension => geometry.DimensionType is not null &&
                geometry.DimensionStyle is not null,
            InspectionGeometryKind.Hatch => geometry.PatternName is not null,
            InspectionGeometryKind.BlockReference => geometry.BlockName is not null &&
                geometry.Insertion is not null &&
                geometry.ScaleX is not null &&
                geometry.ScaleY is not null,
            _ => false,
        };
    }

    private JsonArray Point(InspectionPoint point) => new(Scale(point.X), Scale(point.Y));

    private double Scale(double value) => FiniteProduct(value, _coordinateScale);

    private static double RadiansToDegrees(double value) => FiniteProduct(value, 180.0 / Math.PI);

    private static double FiniteProduct(params double[] factors)
    {
        var result = 1.0;
        foreach (var factor in factors)
        {
            result *= factor;
            if (!double.IsFinite(result))
            {
                throw new InvalidOperationException("Observed geometry cannot be represented as finite JSON.");
            }
        }

        return result;
    }

    private static JsonArray References(IReadOnlyList<StableEntityReference>? references) =>
        references is null
            ? new JsonArray()
            : Strings(references.Select(ToWireEntityReference));

    private static JsonArray Layers(
        IReadOnlyList<InspectionLayer> layers,
        CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(layers);
        var result = new JsonArray();
        foreach (var layer in layers)
        {
            cancellationToken.ThrowIfCancellationRequested();
            result.Add(new JsonObject
            {
                ["name"] = layer.Name,
                ["color_index"] = layer.ColorIndex,
                ["linetype"] = layer.Linetype,
                ["lineweight"] = null,
                ["frozen"] = layer.IsFrozen,
                ["off"] = layer.IsOff,
                ["locked"] = layer.IsLocked,
            });
        }

        return result;
    }

    private static JsonArray Styles(
        IReadOnlyList<InspectionStyle> styles,
        InspectionStyleKind kind,
        CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(styles);
        var result = new JsonArray();
        foreach (var style in styles.Where(style => style.Kind == kind))
        {
            cancellationToken.ThrowIfCancellationRequested();
            result.Add(style.Name);
        }

        return result;
    }

    private static JsonObject Counts(
        IEnumerable<InspectionEntity> entities,
        Func<InspectionEntity, string> selector)
    {
        var result = new JsonObject();
        foreach (var group in entities.GroupBy(selector, StringComparer.Ordinal).OrderBy(group => group.Key, StringComparer.Ordinal))
        {
            result[group.Key] = group.Count();
        }

        return result;
    }

    private static JsonArray Unsupported(IReadOnlyDictionary<string, int> counts)
    {
        var result = new JsonArray();
        foreach (var pair in counts.OrderBy(pair => pair.Key, StringComparer.Ordinal))
        {
            result.Add(new JsonObject
            {
                ["entity_type"] = pair.Key,
                ["count"] = pair.Value,
            });
        }

        return result;
    }

    private static JsonArray Strings(IEnumerable<string> values) => new(
        values.Select(value => JsonValue.Create(value)).ToArray());

    private static string EntityType(InspectionEntity entity) => entity.Geometry.Kind switch
    {
        InspectionGeometryKind.Point => "AcDbPoint",
        InspectionGeometryKind.Line => "AcDbLine",
        InspectionGeometryKind.Circle => "AcDbCircle",
        InspectionGeometryKind.Arc => "AcDbArc",
        InspectionGeometryKind.Ellipse => "AcDbEllipse",
        InspectionGeometryKind.Polyline when string.Equals(
            entity.EntityType,
            "POLYLINE",
            StringComparison.OrdinalIgnoreCase) => "AcDb2dPolyline",
        InspectionGeometryKind.Polyline => "AcDbPolyline",
        InspectionGeometryKind.Text when string.Equals(entity.EntityType, "MTEXT", StringComparison.OrdinalIgnoreCase) => "AcDbMText",
        InspectionGeometryKind.Text => "AcDbText",
        InspectionGeometryKind.Dimension => "AcDbDimension",
        InspectionGeometryKind.Hatch => "AcDbHatch",
        InspectionGeometryKind.BlockReference => "AcDbBlockReference",
        _ => entity.EntityType,
    };

    private static string WireSpace(string source) => source switch
    {
        "model_space" => "model",
        _ when source.StartsWith("layout:", StringComparison.OrdinalIgnoreCase) =>
            $"paper:{source["layout:".Length..]}",
        _ => source,
    };

    private static string SnapshotUnit(string sourceUnitCode) => sourceUnitCode.Trim().ToLowerInvariant() switch
    {
        "mm" or "millimeter" or "millimeters" => "mm",
        "cm" or "centimeter" or "centimeters" => "cm",
        "m" or "meter" or "meters" => "m",
        "in" or "inch" or "inches" => "in",
        _ => "unknown",
    };

    private static void Increment(IDictionary<string, int> counts, string key) =>
        counts[key] = counts.TryGetValue(key, out var count) ? count + 1 : 1;

    private static void ValidateSnapshot(DocumentInspectionSnapshot snapshot)
    {
        ArgumentNullException.ThrowIfNull(snapshot);
        ValidateText(snapshot.DocumentId, nameof(snapshot.DocumentId));
        ValidateText(snapshot.Revision, nameof(snapshot.Revision));
        ArgumentNullException.ThrowIfNull(snapshot.Entities);
        ArgumentNullException.ThrowIfNull(snapshot.Layers);
        ArgumentNullException.ThrowIfNull(snapshot.Styles);
    }

    private static void ValidatePathHash(string value)
    {
        ArgumentNullException.ThrowIfNull(value);
        const int digestLength = 64;
        if (!value.StartsWith("sha256:", StringComparison.Ordinal) ||
            value.Length != "sha256:".Length + digestLength ||
            value["sha256:".Length..].Any(character => !Uri.IsHexDigit(character)))
        {
            throw new ArgumentException("PathHash must be a redacted SHA-256 digest.", nameof(value));
        }
    }

    private static void ValidateText(string value, string parameterName)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(value, parameterName);
        if (value.Any(char.IsControl))
        {
            throw new ArgumentException("Text values must not contain control characters.", parameterName);
        }
    }

    private static void ValidateOptionalText(string? value, string parameterName)
    {
        if (value is not null)
        {
            ValidateText(value, parameterName);
        }
    }
}
