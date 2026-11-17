"""Shared CPU budget for native OCR calls; independent of Tk and providers."""
from contextlib import contextmanager
import os
import threading

from wordlookup.network.budget import check_cancelled


# Reentrant because preparation and adapters may both reserve the same slot.
# Every engine shares this gate, including hover, sentence and frame workers.
OCR_GATE = threading.RLock()


@contextmanager
def ocr_slot():
    check_cancelled()
    while not OCR_GATE.acquire(timeout=0.05):
        check_cancelled()
    try:
        check_cancelled()
        yield
        # Native inference cannot be safely killed from another Python thread.
        # Reject its obsolete result immediately after it returns instead.
        check_cancelled()
    finally:
        OCR_GATE.release()


def bounded_number(settings, key, default, minimum, maximum):
    try:
        value = float(settings.get(key, default))
    except (ValueError, TypeError):
        value = default
    if value != value:  # JSON/manual configuration may contain NaN.
        value = default
    return max(minimum, min(maximum, value))


def cpu_threads(settings):
    return min(os.cpu_count() or 1,
               int(bounded_number(settings, "cpu_threads", 2, 1, 4)))


def max_side(settings):
    return int(bounded_number(settings, "max_side", 1600, 640, 3200))


def fit_image(image, settings, upscale=1.0):
    """Resize once, preserving exact per-axis factors for coordinate mapping.

    Small word crops retain their requested upscale, while large screenshots
    never create an intermediate multi-megapixel enlarged copy.
    """
    from PIL import Image
    factor = min(float(upscale), max_side(settings) / max(image.size))
    width = max(1, round(image.width * factor))
    height = max(1, round(image.height * factor))
    if (width, height) == image.size:
        return image, 1.0, 1.0
    work = image.resize((width, height), Image.Resampling.LANCZOS)
    return work, width / image.width, height / image.height


@contextmanager
def _quiet_onnx_sessions(session_type):
    """Scoped adapter for pinned RapidOCR 1.4.4's missing session-options hook.

    Only its OCR session factory is wrapped, under the shared construction gate;
    ONNX Runtime classes and the process environment are never changed. Restore
    even on failed model loading. Drop this adapter when upstream exposes a
    supported session-options argument, and validate before updating the pin.
    """
    original_descriptor = session_type.__dict__["_init_sess_opts"]
    original = session_type._init_sess_opts

    def quiet_options(config):
        options = original(config)
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        options.add_session_config_entry("session.inter_op.allow_spinning", "0")
        return options

    session_type._init_sess_opts = staticmethod(quiet_options)
    try:
        yield
    finally:
        session_type._init_sess_opts = original_descriptor


def create_rapid_engine(threads=2, limit=1600):
    """Construct the supported backend with bounded threads and no idle spin."""
    from importlib.metadata import version
    if version("rapidocr-onnxruntime") != "1.4.4":
        raise RuntimeError("Для обмеження CPU встанови rapidocr-onnxruntime==1.4.4")
    from rapidocr_onnxruntime import RapidOCR
    from rapidocr_onnxruntime.utils.infer_engine import OrtInferSession
    with ocr_slot(), _quiet_onnx_sessions(OrtInferSession):
        import onnxruntime
        import cv2
        threads = min(os.cpu_count() or 1, max(1, min(4, int(threads))))
        onnxruntime.disable_telemetry_events()
        # OpenCV preprocessing also owns a native pool, independent of ONNX.
        cv2.setNumThreads(threads)
        cv2.ocl.setUseOpenCL(False)
        return RapidOCR(intra_op_num_threads=threads,
                        inter_op_num_threads=1, max_side_len=int(limit),
                        use_cls=False, det_limit_type="max", det_use_cuda=False, cls_use_cuda=False,
                        rec_use_cuda=False)
