"""流水线包入口：把核心引擎类导出到更短的路径上，方便外部 import。

- FaceRecognitionEngine：当前正式类名。
- ImagePipeline：早期 Phase 4 用的旧名，保留为别名以免破坏已有调用方。
"""

from face_engine.pipeline.image_pipeline import FaceRecognitionEngine, ImagePipeline

__all__ = ["FaceRecognitionEngine", "ImagePipeline"]
