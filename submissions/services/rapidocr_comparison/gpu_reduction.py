"""CUDA CTC reduction; original weights, decoder and exceptional NumPy semantics.

The weight-free graph runs ArgMax(axis=2, first tie), ReduceMax(axis=2) and a
nonfinite-input flag. One bounded session is shared by all page workers.
"""
import base64
from dataclasses import dataclass
import hashlib
import threading

import numpy as np

# IR 8 / opset 18. Embedded to avoid a runtime ONNX compiler dependency.
GRAPH = base64.b64decode(
    "CAg61AMKVQoKcHJlZGljdGlvbhIHaW5kaWNlcyIGQXJnTWF4KgsKBGF4aXMYAqABAioPCghrZWVwZGltcxgAoAECKhgKEXNlbGVjdF9sYXN0X2luZGV4GACgAQIKPAoKcHJlZGljdGlvbgoKY2xhc3NfYXhpcxIGc2NvcmVzIglSZWR1Y2VNYXgqDwoIa2VlcGRpbXMYAKABAgoYCgpwcmVkaWN0aW9uEgNuYW4iBUlzTmFOChgKCnByZWRpY3Rpb24SA2luZiIFSXNJbmYKGQoDbmFuCgNpbmYSCW5vbmZpbml0ZSICT3IKIwoJbm9uZmluaXRlEgVmbGFncyIEQ2FzdCoJCgJ0bxgCoAECCi4KBWZsYWdzEglleGNlcHRpb24iCVJlZHVjZU1heCoPCghrZWVwZGltcxgAoAECEhFjdGNfYXJnbWF4X21heF92MSoTCAEQBzoBAkIKY2xhc3NfYXhpc1ojCgpwcmVkaWN0aW9uEhUKEwgBEg8KAxIBTgoDEgFUCgMSAUNiGwoHaW5kaWNlcxIQCg4IBxIKCgMSAU4KAxIBVGIaCgZzY29yZXMSEAoOCAESCgoDEgFOCgMSAVRiEwoJZXhjZXB0aW9uEgYKBAgCEgBCBAoAEBI="
)
GRAPH_SHA256 = "39c14e18160be2ae5849966cd0a5f0fa5622af48f8724952339728ae753b7abb"


@dataclass(frozen=True)
class ReducedPrediction:
    indices: np.ndarray
    scores: np.ndarray


class ReductionPool:
    def __init__(self):
        import onnxruntime as ort
        if hashlib.sha256(GRAPH).hexdigest() != GRAPH_SHA256:
            raise ValueError("Invalid CTC reduction graph.")
        ort.preload_dlls(directory="")
        options = ort.SessionOptions()
        options.intra_op_num_threads = options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(GRAPH, sess_options=options, providers=[
            ("CUDAExecutionProvider", {"device_id": 0, "use_tf32": 0,
                "arena_extend_strategy": "kNextPowerOfTwo", "gpu_mem_limit": 1536 * 1024 * 1024}),
        ])
        if self.session.get_providers()[0] != "CUDAExecutionProvider":
            raise ValueError("CUDA CTC reduction fell back to CPU.")
        self.session.disable_fallback()
        self.run_options = ort.RunOptions()
        self.run_options.add_run_config_entry("disable_synchronize_execution_providers", "0")
        self.lock = threading.Lock()

    def convert(self, prediction):
        if len(prediction.shape()) != 3 or prediction.data_type() != "tensor(float)" or min(prediction.shape()) <= 0:
            raise ValueError("Invalid recognition tensor for CTC reduction.")
        with self.lock:
            binding = self.session.io_binding()
            binding.bind_ortvalue_input("prediction", prediction)
            for name in ("indices", "scores", "exception"):
                binding.bind_output(name, "cpu")
            self.session.run_with_iobinding(binding, self.run_options)
            indices, scores, exception = binding.copy_outputs_to_cpu()
        # CUDA does not share every NumPy NaN/Inf/signed-zero reduction rule.
        if (bool(exception.item()) or not np.isfinite(scores).all()
                or (np.abs(scores) < np.finfo(np.float32).tiny).any()):
            return prediction.numpy()
        return ReducedPrediction(indices, scores)


class RecognitionSession:
    def __init__(self, original, reduction):
        self.original, self.reduction = original, reduction

    def __getattr__(self, name):
        return getattr(self.original, name)

    def __call__(self, inputs):
        session = self.original.session
        binding = session.io_binding()
        names, outputs = self.original.get_input_names(), self.original.get_output_names()
        if len(names) != 1 or len(outputs) != 1:
            raise ValueError("Unsupported recognition model input/output contract.")
        binding.bind_cpu_input(names[0], inputs)
        binding.bind_output(outputs[0], "cuda", 0)
        # Default synchronous Run completion is deliberate; additional explicit
        # synchronize_outputs would impose an unnecessary device-wide sync.
        session.run_with_iobinding(binding)
        prediction, = binding.get_outputs()
        return self.reduction.convert(prediction)


class DecodeAdapter:
    def __init__(self, original):
        self.original = original

    def __getattr__(self, name):
        return getattr(self.original, name)

    def __call__(self, prediction, return_word_box=False, **kwargs):
        if not isinstance(prediction, ReducedPrediction):
            return self.original(prediction, return_word_box, **kwargs)
        return self.original.decode(prediction.indices, prediction.scores, return_word_box,
            kwargs.get("wh_ratio_list", (1.0,)), kwargs.get("max_wh_ratio", 1.0), remove_duplicate=True)


def attach(engine, reduction):
    recognizer = engine._load_rec_model()
    recognizer.session = RecognitionSession(recognizer.session, reduction)
    recognizer.postprocess_op = DecodeAdapter(recognizer.postprocess_op)
