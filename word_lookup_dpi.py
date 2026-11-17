"""Enable per-monitor physical coordinates before creating any Tk window."""
import sys


def configure_dpi() -> str:
    if sys.platform != 'win32':
        return 'platform-default'
    import ctypes
    from ctypes import wintypes
    user = ctypes.WinDLL('user32', use_last_error=True)
    if hasattr(user, 'SetProcessDpiAwarenessContext'):
        user.SetProcessDpiAwarenessContext.argtypes = [wintypes.HANDLE]
        user.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
        if user.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return 'per-monitor-v2'
    # A manifest or earlier process initialization may already have set DPI mode.
    try:
        result = ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return 'per-monitor' if result == 0 else 'manifest-or-existing'
    except (AttributeError, OSError):
        return 'existing'
