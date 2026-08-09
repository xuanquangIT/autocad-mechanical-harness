using System.Collections;
using CadBridge.Inspection;
using CadBridge.Metadata;
using Xunit;

namespace CadBridge.Tests;

public sealed class BridgeInspectionServiceTests
{
    [Fact]
    public void StableEntityReferencesNormalizeHexHandlesAndRejectNonHexValues()
    {
        Assert.Equal("00ABCDEF", new StableEntityReference("00abcdef").Handle);
        Assert.Throws<ArgumentException>(() => new StableEntityReference("not-a-handle"));
    }

    [Fact]
    public void DocumentRevisionAndSnapshotsAreIndependentOfHostEnumerationOrder()
    {
        var first = CreateRichDocument(reverse: false, sourceUnitCode: "Millimeters");
        var second = CreateRichDocument(reverse: true, sourceUnitCode: " MILLIMETERS ");

        var firstSnapshot = new BridgeInspectionService(first).InspectDocument();
        var secondSnapshot = new BridgeInspectionService(second).InspectDocument();

        Assert.Equal(firstSnapshot.Revision, secondSnapshot.Revision);
        Assert.Matches("^sha256:[0-9a-f]{64}$", firstSnapshot.Revision);
        Assert.Equal("millimeters", firstSnapshot.SourceUnitCode);
        Assert.Equal("millimeters", secondSnapshot.SourceUnitCode);
        Assert.Equal(new[] { "0A", "0B", "0C", "0D" }, firstSnapshot.Entities.Select(entity => entity.EntityRef.Handle));
        Assert.Equal(new[] { "0", "DETAIL" }, firstSnapshot.Layers.Select(layer => layer.Name));
        Assert.Equal(
            new[] { (InspectionStyleKind.Text, "ANNOTATION"), (InspectionStyleKind.Dimension, "ISO-25") },
            firstSnapshot.Styles.Select(style => (style.Kind, style.Name)));
        Assert.Equal(1, first.EntityReads);
        Assert.Equal(1, first.LayerReads);
        Assert.Equal(1, first.StyleReads);
        Assert.Equal(0, first.SelectionReads);
    }


    [Fact]
    public void SourceUnitCodeChangesRevisionWithoutChangingObservedDrawingContent()
    {
        var millimeters = CreateRichDocument(reverse: false, sourceUnitCode: "Millimeters");
        var inches = CreateRichDocument(reverse: false, sourceUnitCode: "Inches");

        var millimeterSnapshot = new BridgeInspectionService(millimeters).InspectDocument();
        var inchSnapshot = new BridgeInspectionService(inches).InspectDocument();

        Assert.Equal("millimeters", millimeterSnapshot.SourceUnitCode);
        Assert.Equal("inches", inchSnapshot.SourceUnitCode);
        Assert.NotEqual(millimeterSnapshot.Revision, inchSnapshot.Revision);
        Assert.Equal(
            millimeterSnapshot.Entities.Select(entity => entity.EntityRef.Handle),
            inchSnapshot.Entities.Select(entity => entity.EntityRef.Handle));
        Assert.Equal(
            millimeterSnapshot.Layers.Select(layer => layer.Name),
            inchSnapshot.Layers.Select(layer => layer.Name));
        Assert.Equal(
            millimeterSnapshot.Styles.Select(style => (style.Kind, style.Name)),
            inchSnapshot.Styles.Select(style => (style.Kind, style.Name)));
    }

    [Fact]
    public void SelectionSnapshotUsesStableHandlesAndCanonicalEntityOrder()
    {
        var document = CreateRichDocument(reverse: true);
        document.Selection = new[] { new StableEntityReference("0d"), new StableEntityReference("0a") };

        var selection = new BridgeInspectionService(document).InspectSelection();

        Assert.Equal(new[] { "0A", "0D" }, selection.Entities.Select(entity => entity.EntityRef.Handle));
        Assert.Equal(new BridgeInspectionService(CreateRichDocument(reverse: false)).InspectDocument().Revision, selection.Revision);
        Assert.Equal(1, document.SelectionReads);
    }

    [Fact]
    public void BoundedInspectionStopsAtFirstEntityBeyondBudget()
    {
        var entities = new CountingEnumerable<InspectionEntity>(Enumerable.Range(1, 5).Select(index =>
            Entity(index.ToString("X"), InspectionGeometryKind.Point, [new InspectionPoint(index, 0)])));
        var document = new RecordingDocument("doc-bounded", entities, [], []);

        var exception = Assert.Throws<InvalidOperationException>(() =>
            new BridgeInspectionService(document).InspectDocumentBounded(2, _ => true));

        Assert.Contains("max_entities", exception.Message, StringComparison.Ordinal);
        Assert.Equal(3, entities.EnumeratedCount);
    }

    [Fact]
    public void BoundedInspectionRevisionStillCommitsExcludedGeometry()
    {
        static RecordingDocument Document(double excludedX) => new(
            "doc-revision",
            [
                Entity("0A", InspectionGeometryKind.Point, [new InspectionPoint(1, 0)]),
                Entity("0B", InspectionGeometryKind.Point, [new InspectionPoint(excludedX, 0)]),
            ],
            [],
            []);

        var first = new BridgeInspectionService(Document(2)).InspectDocumentBounded(
            1,
            entity => entity.EntityRef.Handle == "0A");
        var second = new BridgeInspectionService(Document(3)).InspectDocumentBounded(
            1,
            entity => entity.EntityRef.Handle == "0A");

        Assert.Single(first.Entities);
        Assert.Equal("0A", first.Entities[0].EntityRef.Handle);
        Assert.NotEqual(first.Revision, second.Revision);
    }

    [Fact]
    public void BoundedInspectionRejectsAtHardWholeDocumentObservationCap()
    {
        var entities = new CountingEnumerable<InspectionEntity>(Enumerable
            .Range(1, BridgeInspectionService.MaximumWholeDocumentObservationEntities + 1)
            .Select(index => Entity(
                index.ToString("X"),
                InspectionGeometryKind.Point,
                [new InspectionPoint(index, 0)])));
        var document = new RecordingDocument("doc-hard-cap", entities, [], []);

        var exception = Assert.Throws<InvalidOperationException>(() =>
            new BridgeInspectionService(document).InspectDocumentBounded(1, _ => false));

        Assert.Contains("hard observation limit", exception.Message, StringComparison.Ordinal);
        Assert.Equal(
            BridgeInspectionService.MaximumWholeDocumentObservationEntities + 1,
            entities.EnumeratedCount);
    }

    [Fact]
    public void OversizedSelectionFailsBeforeReadingDocumentGeometry()
    {
        var document = CreateRichDocument(reverse: false);
        document.Selection =
        [
            new StableEntityReference("0A"),
            new StableEntityReference("0B"),
        ];

        var exception = Assert.Throws<InvalidOperationException>(() =>
            new BridgeInspectionService(document).InspectSelection(1));

        Assert.Contains("max_entities", exception.Message, StringComparison.Ordinal);
        Assert.Equal(0, document.EntityReads);
    }

    [Fact]
    public void MeasurementsAreDerivedFromObservedHostGeometryAndPreserveHostBounds()
    {
        var service = new BridgeInspectionService(CreateRichDocument(reverse: true));

        var snapshot = service.MeasureCreatedEntities(
            new[]
            {
                new StableEntityReference("0d"),
                new StableEntityReference("0b"),
                new StableEntityReference("0a"),
                new StableEntityReference("0c"),
            });

        Assert.Equal(new[] { "0A", "0B", "0C", "0D" }, snapshot.Measurements.Select(measurement => measurement.EntityRef.Handle));

        var line = snapshot.Measurements[0];
        Assert.Equal(13.0, line.Length);
        Assert.Null(line.Area);
        Assert.Null(line.Radius);
        Assert.Equal(new InspectionPoint(-1, -2, -3), line.Bounds!.Minimum);
        Assert.Equal(new InspectionPoint(4, 5, 13), line.Bounds.Maximum);

        var circle = snapshot.Measurements[1];
        AssertClose(4.0 * Math.PI, circle.Length);
        AssertClose(4.0 * Math.PI, circle.Area);
        Assert.Equal(2.0, circle.Radius);
        Assert.Equal(new InspectionPoint(8, 18, 3), circle.Bounds!.Minimum);
        Assert.Equal(new InspectionPoint(12, 22, 3), circle.Bounds.Maximum);

        var polyline = snapshot.Measurements[2];
        Assert.Equal(12.0, polyline.Length);
        Assert.Equal(6.0, polyline.Area);
        Assert.Equal(new InspectionPoint(0, 0, 0), polyline.Bounds!.Minimum);
        Assert.Equal(new InspectionPoint(3, 4, 0), polyline.Bounds.Maximum);

        var arc = snapshot.Measurements[3];
        AssertClose(Math.PI, arc.Length);
        Assert.Null(arc.Area);
        Assert.Equal(2.0, arc.Radius);
        AssertPointClose(new InspectionPoint(1, 2, 5), arc.Bounds!.Minimum);
        AssertPointClose(new InspectionPoint(3, 4, 5), arc.Bounds.Maximum);
    }

    [Fact]
    public void CurvedPolylineAndEllipseMeasurementsUseObservedGeometry()
    {
        var curved = Entity(
            "10",
            InspectionGeometryKind.Polyline,
            new[] { new InspectionPoint(-1, 0), new InspectionPoint(1, 0) },
            bulges: new[] { 1.0, 0.0 });
        var ellipse = new InspectionEntity(
            new StableEntityReference("11"),
            "Ellipse",
            "0",
            null,
            7,
            "Continuous",
            null,
            new InspectionGeometry(
                InspectionGeometryKind.Ellipse,
                new[] { new InspectionPoint(0, 0) },
                MajorAxis: 4.0,
                MinorAxis: 2.0,
                RotationRadians: 0.0));
        var document = new RecordingDocument(
            "drawing-curves",
            new[] { curved, ellipse },
            Array.Empty<InspectionLayer>(),
            Array.Empty<InspectionStyle>());

        var measured = new BridgeInspectionService(document).MeasureCreatedEntities(
            new[] { new StableEntityReference("10"), new StableEntityReference("11") });

        AssertClose(Math.PI, measured.Measurements[0].Length);
        AssertClose(Math.PI * 8.0, measured.Measurements[1].Area);
        Assert.True(measured.Measurements[1].Length > 19.0);
    }

    [Fact]
    public void RevisionIncludesBulgesVisibilityAndDrawingSpace()
    {
        static string Revision(double bulge, bool visible, string space)
        {
            var entity = Entity(
                "20",
                InspectionGeometryKind.Polyline,
                new[] { new InspectionPoint(0, 0), new InspectionPoint(2, 0) },
                bulges: new[] { bulge, 0.0 },
                visible: visible,
                space: space);
            var document = new RecordingDocument(
                "drawing-revision",
                new[] { entity },
                Array.Empty<InspectionLayer>(),
                Array.Empty<InspectionStyle>());
            return new BridgeInspectionService(document).InspectDocument().Revision;
        }

        Assert.NotEqual(Revision(0.0, true, "model_space"), Revision(0.5, true, "model_space"));
        Assert.NotEqual(Revision(0.0, true, "model_space"), Revision(0.0, false, "model_space"));
        Assert.NotEqual(Revision(0.0, true, "model_space"), Revision(0.0, true, "layout:Sheet1"));
    }

    [Fact]
    public void NestedChildGeometryContentStyleVisibilitySpaceAndMetadataAffectRevision()
    {
        var baselineChild = Entity(
            "31",
            InspectionGeometryKind.Line,
            new[] { new InspectionPoint(0, 0), new InspectionPoint(1, 0) },
            metadata: CadHarnessMetadata.Create("feature-child", "operation-child")) with
        {
            Content = "baseline-content",
            Style = "CHILD-STYLE",
        };
        var baseline = NestedRevision([baselineChild]);
        var mutations = new[]
        {
            baselineChild with
            {
                Geometry = baselineChild.Geometry with
                {
                    Points = new[] { new InspectionPoint(0, 0), new InspectionPoint(2, 0) },
                },
            },
            baselineChild with { Content = "changed-content" },
            baselineChild with { Style = "CHANGED-STYLE" },
            baselineChild with { Visible = false },
            baselineChild with { Space = "layout:Sheet1" },
            baselineChild with
            {
                HarnessMetadata = CadHarnessMetadata.Create("feature-changed", "operation-child"),
            },
        };

        Assert.All(mutations, mutation => Assert.NotEqual(baseline, NestedRevision([mutation])));
    }

    [Fact]
    public void NestedChildEnumerationOrderIsCanonicalAndRevisionInvariant()
    {
        var first = Entity(
            "32",
            InspectionGeometryKind.Line,
            new[] { new InspectionPoint(0, 0), new InspectionPoint(1, 0) });
        var second = Entity(
            "31",
            InspectionGeometryKind.Line,
            new[] { new InspectionPoint(0, 1), new InspectionPoint(1, 1) });

        var forward = InspectNested([first, second]);
        var reverse = InspectNested([second, first]);

        Assert.Equal(forward.Revision, reverse.Revision);
        Assert.Equal(
            new[] { "31", "32" },
            forward.Entities[0].Geometry.ChildEntities!.Select(child => child.EntityRef.Handle));
        Assert.Equal(
            new[] { "31", "32" },
            reverse.Entities[0].Geometry.ChildEntities!.Select(child => child.EntityRef.Handle));
    }

    [Fact]
    public void NestedEntityCyclesAndExcessiveDepthFailClosed()
    {
        var cyclicChildren = new List<InspectionEntity>();
        var cyclicParent = Block("40", cyclicChildren);
        cyclicChildren.Add(cyclicParent);
        var cyclicDocument = DocumentWith(cyclicParent);
        Assert.Throws<InvalidOperationException>(() =>
            new BridgeInspectionService(cyclicDocument).InspectDocument());

        var nested = Entity(
            "50",
            InspectionGeometryKind.Point,
            new[] { new InspectionPoint(0, 0) });
        for (var depth = 0; depth < 34; depth++)
        {
            nested = Block((0x51 + depth).ToString("X"), [nested]);
        }

        var deepDocument = DocumentWith(nested);
        Assert.Throws<InvalidOperationException>(() =>
            new BridgeInspectionService(deepDocument).InspectDocument());
    }

    [Fact]
    public void SnapshotCopiesHostPointCollectionsAndExposesNoMutationContract()
    {
        var hostPoints = new[] { new InspectionPoint(0, 0), new InspectionPoint(1, 1) };
        var document = new RecordingDocument(
            "drawing-copy",
            new[] { Entity("aa", InspectionGeometryKind.Line, hostPoints) },
            Array.Empty<InspectionLayer>(),
            Array.Empty<InspectionStyle>());

        var snapshot = new BridgeInspectionService(document).InspectDocument();
        hostPoints[0] = new InspectionPoint(999, 999);

        Assert.Equal(new InspectionPoint(0, 0), snapshot.Entities[0].Geometry.Points[0]);
        Assert.Equal(
            new[]
            {
                "get_DocumentId",
                "get_SourceUnitCode",
                "ReadEntities",
                "ReadEntities",
                "ReadLayers",
                "ReadSelection",
                "ReadStyles",
            },
            typeof(IBoundInspectionDocument).GetMethods().Select(method => method.Name).Order());
    }

    [Fact]
    public void CancellationIsObservedInsideHostEnumerationAndBeforeLaterCollectionsAreRead()
    {
        using var cancellation = new CancellationTokenSource();
        var entities = new CancelAfterFirstEnumerable<InspectionEntity>(
            new[]
            {
                Entity("01", InspectionGeometryKind.Point, new[] { new InspectionPoint(0, 0) }),
                Entity("02", InspectionGeometryKind.Point, new[] { new InspectionPoint(1, 1) }),
            },
            cancellation);
        var document = new RecordingDocument(
            "drawing-cancel",
            entities,
            new[] { new InspectionLayer("0", false, false, false, 7, "Continuous") },
            Array.Empty<InspectionStyle>());

        Assert.Throws<OperationCanceledException>(() =>
            new BridgeInspectionService(document).InspectDocument(cancellation.Token));
        Assert.Equal(1, document.EntityReads);
        Assert.Equal(0, document.LayerReads);
        Assert.Equal(0, document.StyleReads);
    }

    [Fact]
    public void TokenAwareEntityEnumerationReceivesTheInspectionCancellationToken()
    {
        using var cancellation = new CancellationTokenSource();
        var document = CreateRichDocument(reverse: false);
        CancellationToken observed = default;
        document.OnReadEntitiesWithCancellation = token => observed = token;

        _ = new BridgeInspectionService(document).InspectDocument(cancellation.Token);

        Assert.Equal(cancellation.Token, observed);
    }

    [Fact]
    public void CancellationIsObservedInsideSelectionAndMeasurementLoops()
    {
        using var selectionCancellation = new CancellationTokenSource();
        var selectionDocument = CreateRichDocument(reverse: false);
        selectionDocument.OnReadSelection = selectionCancellation.Cancel;
        Assert.Throws<OperationCanceledException>(() =>
            new BridgeInspectionService(selectionDocument).InspectSelection(selectionCancellation.Token));

        using var measurementCancellation = new CancellationTokenSource();
        var measurementDocument = CreateRichDocument(reverse: false);
        measurementDocument.OnReadStyles = measurementCancellation.Cancel;
        Assert.Throws<OperationCanceledException>(() =>
            new BridgeInspectionService(measurementDocument).MeasureCreatedEntities(
                new[] { new StableEntityReference("0a") },
                measurementCancellation.Token));
    }

    private static RecordingDocument CreateRichDocument(
        bool reverse,
        string sourceUnitCode = "unitless")
    {
        var entities = new[]
        {
            Entity(
                "0a",
                InspectionGeometryKind.Line,
                new[] { new InspectionPoint(0, 0, 0), new InspectionPoint(3, 4, 12) },
                bounds: new InspectionBounds(new InspectionPoint(-1, -2, -3), new InspectionPoint(4, 5, 13)),
                metadata: CadHarnessMetadata.Create("feature-line", "operation-line")),
            Entity(
                "0b",
                InspectionGeometryKind.Circle,
                new[] { new InspectionPoint(10, 20, 3) },
                radius: 2.0),
            Entity(
                "0c",
                InspectionGeometryKind.Polyline,
                new[] { new InspectionPoint(0, 0), new InspectionPoint(3, 0), new InspectionPoint(3, 4) },
                closed: true),
            Entity(
                "0d",
                InspectionGeometryKind.Arc,
                new[] { new InspectionPoint(1, 2, 5) },
                radius: 2.0,
                startAngle: 0.0,
                endAngle: Math.PI / 2.0),
        };
        var layers = new[]
        {
            new InspectionLayer("DETAIL", false, false, true, 1, "Hidden"),
            new InspectionLayer("0", false, false, false, 7, "Continuous"),
        };
        var styles = new[]
        {
            new InspectionStyle("ISO-25", InspectionStyleKind.Dimension, false, 1.0),
            new InspectionStyle("ANNOTATION", InspectionStyleKind.Text, true, 2.5),
        };
        if (reverse)
        {
            Array.Reverse(entities);
            Array.Reverse(layers);
            Array.Reverse(styles);
        }

        return new RecordingDocument("drawing-01", entities, layers, styles)
        {
            Selection = new[] { new StableEntityReference("0a") },
            SourceUnitCode = sourceUnitCode,
        };
    }

    private static string NestedRevision(IReadOnlyList<InspectionEntity> children) =>
        InspectNested(children).Revision;

    private static DocumentInspectionSnapshot InspectNested(
        IReadOnlyList<InspectionEntity> children) =>
        new BridgeInspectionService(DocumentWith(Block("30", children))).InspectDocument();

    private static RecordingDocument DocumentWith(InspectionEntity entity) =>
        new(
            "drawing-nested-revision",
            new[] { entity },
            Array.Empty<InspectionLayer>(),
            Array.Empty<InspectionStyle>());

    private static InspectionEntity Block(
        string handle,
        IReadOnlyList<InspectionEntity> children) =>
        new(
            new StableEntityReference(handle),
            "BlockReference",
            "0",
            null,
            7,
            "Continuous",
            null,
            new InspectionGeometry(
                InspectionGeometryKind.BlockReference,
                Array.Empty<InspectionPoint>(),
                BlockName: "NESTED-BLOCK",
                Insertion: new InspectionPoint(0, 0),
                ScaleX: 1.0,
                ScaleY: 1.0,
                NestedDepthRead: 1,
                ChildEntities: children),
            null,
            true,
            "model_space");

    private static InspectionEntity Entity(
        string handle,
        InspectionGeometryKind kind,
        IReadOnlyList<InspectionPoint> points,
        InspectionBounds? bounds = null,
        double? radius = null,
        double? startAngle = null,
        double? endAngle = null,
        bool closed = false,
        CadHarnessMetadata? metadata = null,
        IReadOnlyList<double>? bulges = null,
        bool visible = true,
        string space = "model_space") =>
        new(
            new StableEntityReference(handle),
            kind.ToString(),
            "0",
            null,
            7,
            "Continuous",
            null,
            new InspectionGeometry(kind, points, bounds, radius, startAngle, endAngle, closed, bulges),
            metadata,
            visible,
            space);

    private static void AssertClose(double expected, double? actual) =>
        Assert.Equal(expected, Assert.IsType<double>(actual), 12);

    private static void AssertPointClose(InspectionPoint expected, InspectionPoint actual)
    {
        Assert.Equal(expected.X, actual.X, 12);
        Assert.Equal(expected.Y, actual.Y, 12);
        Assert.Equal(expected.Z, actual.Z, 12);
    }

    private sealed class RecordingDocument : IBoundInspectionDocument
    {
        private readonly IEnumerable<InspectionEntity> _entities;
        private readonly IEnumerable<InspectionLayer> _layers;
        private readonly IEnumerable<InspectionStyle> _styles;

        public RecordingDocument(
            string documentId,
            IEnumerable<InspectionEntity> entities,
            IEnumerable<InspectionLayer> layers,
            IEnumerable<InspectionStyle> styles)
        {
            DocumentId = documentId;
            _entities = entities;
            _layers = layers;
            _styles = styles;
        }

        public string DocumentId { get; }

        public string SourceUnitCode { get; init; } = "unitless";

        public IReadOnlyCollection<StableEntityReference> Selection { get; set; } = Array.Empty<StableEntityReference>();

        public Action? OnReadSelection { get; set; }

        public Action? OnReadStyles { get; set; }

        public Action<CancellationToken>? OnReadEntitiesWithCancellation { get; set; }

        public int EntityReads { get; private set; }

        public int LayerReads { get; private set; }

        public int StyleReads { get; private set; }

        public int SelectionReads { get; private set; }

        public IEnumerable<InspectionEntity> ReadEntities()
        {
            EntityReads++;
            return _entities;
        }

        public IEnumerable<InspectionEntity> ReadEntities(CancellationToken cancellationToken)
        {
            cancellationToken.ThrowIfCancellationRequested();
            EntityReads++;
            OnReadEntitiesWithCancellation?.Invoke(cancellationToken);
            return _entities;
        }

        public IEnumerable<InspectionLayer> ReadLayers()
        {
            LayerReads++;
            return _layers;
        }

        public IEnumerable<InspectionStyle> ReadStyles()
        {
            StyleReads++;
            OnReadStyles?.Invoke();
            return _styles;
        }

        public IReadOnlyCollection<StableEntityReference> ReadSelection()
        {
            SelectionReads++;
            OnReadSelection?.Invoke();
            return Selection;
        }
    }

    private sealed class CancelAfterFirstEnumerable<T> : IEnumerable<T>
    {
        private readonly IReadOnlyList<T> _items;
        private readonly CancellationTokenSource _cancellation;

        public CancelAfterFirstEnumerable(IReadOnlyList<T> items, CancellationTokenSource cancellation)
        {
            _items = items;
            _cancellation = cancellation;
        }

        public IEnumerator<T> GetEnumerator()
        {
            for (var index = 0; index < _items.Count; index++)
            {
                if (index == 1)
                {
                    _cancellation.Cancel();
                }

                yield return _items[index];
            }
        }

        IEnumerator IEnumerable.GetEnumerator() => GetEnumerator();
    }

    private sealed class CountingEnumerable<T> : IEnumerable<T>
    {
        private readonly IEnumerable<T> _items;

        public CountingEnumerable(IEnumerable<T> items)
        {
            _items = items;
        }

        public int EnumeratedCount { get; private set; }

        public IEnumerator<T> GetEnumerator()
        {
            foreach (var item in _items)
            {
                EnumeratedCount++;
                yield return item;
            }
        }

        IEnumerator IEnumerable.GetEnumerator() => GetEnumerator();
    }
}
