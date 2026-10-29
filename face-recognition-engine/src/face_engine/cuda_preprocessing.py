"""Optional CUDA normalization and ORT binding; OpenCV resize stays unchanged."""
import cv2
import numpy as np


class CUDAInput:
    def __init__(self, session):
        import torch
        if session.get_providers()[0] != 'CUDAExecutionProvider':
            raise RuntimeError('GPU preprocessing requires an active CUDA model')
        options = session.get_provider_options().get('CUDAExecutionProvider', {})
        self.device_id = int(options.get('device_id', -1))
        if not torch.cuda.is_available() or not 0 <= self.device_id < torch.cuda.device_count():
            raise RuntimeError('GPU preprocessing requires the model CUDA device in Torch')
        self.torch = torch
        self.device = torch.device('cuda', self.device_id)
        self.session = session
        self.session.disable_fallback()
        # Initialize this device eagerly; no silent CPU preprocessing fallback.
        with torch.cuda.device(self.device):
            torch.cuda.init()
        self.output_names = [output.name for output in session.get_outputs()]

    def scrfd(self, image, input_size):
        if image.ndim != 3 or image.shape[2] != 3 or image.dtype != np.uint8:
            raise ValueError('image must be uint8 BGR [H, W, 3]')
        height, width = image.shape[:2]
        if min(height, width) <= 0:
            raise ValueError('image dimensions must be positive')
        input_w, input_h = input_size
        scale = min(input_w / width, input_h / height)
        resized_w, resized_h = max(1, round(width * scale)), max(1, round(height * scale))
        # Keep the exact existing OpenCV uint8 interpolation; no Torch resize.
        resized = cv2.resize(image, (resized_w, resized_h))
        torch = self.torch
        with torch.cuda.device(self.device), torch.no_grad():
            pixels = torch.from_numpy(np.ascontiguousarray(resized)).to(self.device)
            blob = torch.zeros((1, 3, input_h, input_w), device=self.device, dtype=torch.float32)
            blob[0, :, :resized_h, :resized_w].copy_(pixels.permute(2, 0, 1).flip(0))
            blob.sub_(127.5).mul_(1 / 128)
        return blob, scale

    def arcface(self, faces):
        batch = np.asarray(faces)
        if batch.ndim != 4 or batch.shape[1:] != (112, 112, 3) or batch.dtype != np.uint8:
            raise ValueError('aligned faces must be uint8 BGR [N, 112, 112, 3]')
        torch = self.torch
        with torch.cuda.device(self.device), torch.no_grad():
            pixels = torch.from_numpy(np.ascontiguousarray(batch)).to(self.device)
            blob = pixels.permute(0, 3, 1, 2).flip(1).to(dtype=torch.float32).contiguous()
            blob.sub_(127.5).div_(127.5)
        return blob

    def run(self, input_name, blob):
        torch = self.torch
        if blob.device != self.device or blob.dtype != torch.float32 or not blob.is_contiguous():
            raise ValueError('ORT GPU input must be contiguous float32 on the model device')
        # ORT owns its stream. Complete the producing Torch stream before ORT
        # reads the pointer, and retain blob until outputs have been copied.
        with torch.cuda.device(self.device):
            torch.cuda.current_stream(self.device).synchronize()
            binding = self.session.io_binding()
            binding.bind_input(input_name, 'cuda', self.device_id, np.float32,
                               tuple(blob.shape), blob.data_ptr())
            for name in self.output_names:
                binding.bind_output(name, 'cuda', self.device_id)
            self.session.run_with_iobinding(binding)
            binding.synchronize_outputs()
            # Existing box decoding/NMS and embedding validation remain CPU.
            return binding.copy_outputs_to_cpu()


def configure_gpu_preprocessing(extractor, enabled):
    if type(enabled) is not bool:
        raise ValueError('inference_gpu_preprocessing must be boolean')
    if not enabled:
        return

    def attach(model):
        if hasattr(model, 'standard') and hasattr(model, 'small'):
            attach(model.standard)
            attach(model.small)
        else:
            model.cuda_input = CUDAInput(model.session)

    for worker in getattr(extractor, 'extractors', (extractor,)):
        attach(worker.detector)
        attach(worker.recognizer)
