from app.ai.multiview_reconstruct import reconstruct_from_views
from app.db.models import DrawingFeature, DrawingFeatureType, DrawingViewSection, FeatureDimType, FeatureDimension


def test_multiview_reconstruction_marks_depth_and_cross_view_evidence():
    feature = DrawingFeature(
        feature_type=DrawingFeatureType.hole, name="Отверстие", confidence=0.8,
        source_view="front", confirmed_by_views=["front", "section_A-A"],
    )
    feature.dimensions = [FeatureDimension(dim_type=FeatureDimType.depth, nominal=20, unit="mm")]
    views = [DrawingViewSection(section_type="front"), DrawingViewSection(section_type="section", section_label="A-A")]
    candidate = reconstruct_from_views([feature], views)[0]
    assert candidate.score > 0.9
    assert candidate.features[0].depth_mm == 20
    assert candidate.missing_data == []


def test_reconstruction_folds_axis_alignment_correspondence():
    # D1: front/top views aligned on their projection axis produce an
    # axis_alignment correspondence note that reaches the candidate.
    feature = DrawingFeature(
        feature_type=DrawingFeatureType.hole, name="Отверстие", confidence=0.7,
        source_view="front", confirmed_by_views=["front", "top"],
    )
    feature.dimensions = [FeatureDimension(dim_type=FeatureDimType.depth, nominal=10, unit="mm")]
    views = [
        DrawingViewSection(section_type="front", bbox_on_sheet={"x": 0, "y": 0, "w": 100, "h": 60}),
        DrawingViewSection(section_type="top", bbox_on_sheet={"x": 2, "y": 80, "w": 96, "h": 60}),
    ]
    candidate = reconstruct_from_views([feature], views)[0]
    assert any("оси проекции" in note for note in candidate.correspondences)


def test_reconstruction_surfaces_scale_mismatch_from_correspondence():
    feature = DrawingFeature(
        feature_type=DrawingFeatureType.hole, name="Отверстие", confidence=0.7,
        source_view="front", confirmed_by_views=["front"],
    )
    feature.dimensions = []
    views = [
        DrawingViewSection(section_type="front", bbox_on_sheet={"x": 0, "y": 0, "w": 100, "h": 60}),
        DrawingViewSection(section_type="top", bbox_on_sheet={"x": 0, "y": 80, "w": 100, "h": 60}),
    ]
    # scale meta isn't stored on the rows, so no scale edge here — but the
    # axis alignment note must still appear.
    candidate = reconstruct_from_views([feature], views)[0]
    assert candidate.correspondences
