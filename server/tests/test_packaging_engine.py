"""PRD-v2 F7: the render success path does not call Remotion."""
import inspect

from app.services.render import pipeline


def test_packaging_engine_is_ffmpeg_only():
    assert pipeline.packaging_uses_remotion() is False


def test_run_pipeline_does_not_invoke_remotion():
    source = inspect.getsource(pipeline.run_pipeline)
    assert "render_packaging_track" not in source
    assert "remotion_render" not in source
    assert "ffmpeg_packaging" in source


def test_still_image_motion_does_not_import_remotion():
    source = inspect.getsource(pipeline._resolve_aigc_image_scene)
    assert "remotion_renderer" not in source
    assert "image_to_video_with_motion" in source
