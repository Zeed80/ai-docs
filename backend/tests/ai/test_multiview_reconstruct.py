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
