"""Independent, eagerly loaded CUDA extractors for monitoring worker threads."""
import copy
import re
import threading

from face_engine.trajectory_bridge import TrajectoryExtractor


class WorkerExtractors:
    def __init__(self, model_config, devices, worker_count):
        if (not isinstance(devices, list) or not devices
                or any(not isinstance(device, str) or re.fullmatch(r'cuda:[0-9]+', device) is None
                       for device in devices)):
            raise ValueError('inference_devices must be a nonempty list of explicit cuda:<id> devices')
        if type(worker_count) is not int or not 1 <= worker_count <= 16:
            raise ValueError('inference_workers must be 1..16')
        self.worker_devices = tuple(devices[index % len(devices)] for index in range(worker_count))
        self.extractors = []
        self.locks = [threading.RLock() for _ in range(worker_count)]
        for device in self.worker_devices:
            config = copy.deepcopy(model_config)
            config['device'] = device
            extractor = TrajectoryExtractor.from_config(config)
            self._require_cuda(extractor.detector, device)
            self._require_cuda(extractor.recognizer, device)
            if self.extractors and extractor.feature_space != self.extractors[0].feature_space:
                raise RuntimeError('worker extractor feature spaces differ')
            self.extractors.append(extractor)
        # Existing enrollment, gallery metadata and backend health use one prototype.
        self.feature_space = self.extractors[0].feature_space
        self.detector = self.extractors[0].detector
        self.recognizer = self.extractors[0].recognizer
        self.quality_policy = self.extractors[0].quality_policy

    @classmethod
    def _require_cuda(cls, model, device):
        # SizeAwareSCRFDDetector contains two sessions; check both, not just
        # its advertised standard-detector backend.
        if hasattr(model, 'standard') and hasattr(model, 'small'):
            cls._require_cuda(model.standard, device)
            cls._require_cuda(model.small, device)
            return
        session = getattr(model, 'session', None)
        if (getattr(model, 'backend', None) != 'CUDA' or session is None
                or session.get_providers()[0] != 'CUDAExecutionProvider'):
            raise RuntimeError('requested worker CUDA models did not initialize on ' + device)
        options = session.get_provider_options().get('CUDAExecutionProvider', {})
        if int(options.get('device_id', -1)) != int(device.split(':', 1)[1]):
            raise RuntimeError('worker CUDA session initialized on the wrong device')
        # An execution-provider failure must surface, not silently rerun on CPU.
        session.disable_fallback()

    def _index(self):
        name = threading.current_thread().name
        if name == 'monitoring-infer':
            return 0
        if name.startswith('monitoring-infer-'):
            suffix = name[len('monitoring-infer-'):]
            if not suffix.isdigit() or not 0 <= int(suffix) < len(self.extractors):
                raise RuntimeError('inference thread has no assigned extractor')
            return int(suffix)
        # Enrollment or synchronous library calls serialize against worker 0.
        return 0

    def extract_detailed(self, frames, *, select_before_embedding=False):
        index = self._index()
        with self.locks[index]:
            return self.extractors[index].extract_detailed(
                frames, select_before_embedding=select_before_embedding)

    def extract_selected(self, frames):
        return self.extract_detailed(frames, select_before_embedding=True)

    def extract(self, frames):
        return [frame['observations'] for frame in self.extract_detailed(frames)]
