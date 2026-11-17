"""UI intent and Windows handle correctness without touching other applications."""
from types import SimpleNamespace

import word_lookup as w


def test_preview_ignores_late_live_lookup_and_releases_its_pause(app, card):
    released=[]
    class Guard:
        active=True
        def release(self):
            self.active=False
            released.append(True)
            return True,'RESUMED'
    old_token=app._new_token()
    app._lookup_profile={'anki_tag':'previous-game'}
    app._game_pause=Guard()
    app.show_preview()
    assert released==[True]
    assert app.popup.card is w.SAMPLE_CARD
    assert app._lookup_profile=={}
    app._handle(('show',card,None,(100,100,120,120),old_token))
    app._handle(('update',old_token,card['word'],dict(card,translation='late')))
    assert app.popup.card is w.SAMPLE_CARD
    assert app.popup.card['translation']!='late'


def test_changing_language_invalidates_old_lookup_and_preserves_language_cache(app, card):
    old_token=app._new_token()
    w._card_cache['previous-language']=card
    app._setting_vars[('languages','target')].set('Польська')
    assert w.cfg['languages']['target']=='pl'
    assert app.active_token!=old_token
    assert w._card_cache['previous-language']==card
    app._handle(('show',card,None,(100,100,120,120),old_token))
    assert app.popup is None


class NativeFunction:
    def __init__(self, callback):
        self.callback=callback
        self.argtypes=None
        self.restype=None
    def __call__(self,*args):
        return self.callback(*args)


def test_window_title_uses_pointer_sized_handles(monkeypatch):
    import ctypes
    from ctypes import wintypes
    handle=0x123456789
    api=SimpleNamespace()
    def current():
        assert api.GetForegroundWindow.restype is wintypes.HWND
        return handle
    def length(hwnd):
        assert hwnd==handle
        assert api.GetWindowTextLengthW.argtypes==[wintypes.HWND]
        return len('Captured source title')
    def title(hwnd,buf,size):
        assert hwnd==handle
        assert api.GetWindowTextW.argtypes==[wintypes.HWND,wintypes.LPWSTR,ctypes.c_int]
        buf.value='Captured source title'
        return len(buf.value)
    api.GetForegroundWindow=NativeFunction(current)
    api.GetWindowTextLengthW=NativeFunction(length)
    api.GetWindowTextW=NativeFunction(title)
    monkeypatch.setattr(ctypes,'windll',SimpleNamespace(user32=api),raising=False)
    monkeypatch.setattr(w.sys,'platform','win32')
    assert w._foreground_title()=='Captured source title'


def test_focus_restore_reports_windows_refusal(monkeypatch,caplog):
    import ctypes
    api=SimpleNamespace(SetForegroundWindow=NativeFunction(lambda hwnd: 0))
    monkeypatch.setattr(ctypes,'windll',SimpleNamespace(user32=api),raising=False)
    monkeypatch.setattr(w.sys,'platform','win32')
    with caplog.at_level('INFO'):
        assert w._restore_foreground(123) is False
    assert 'Alt+Tab' in caplog.text
    api.SetForegroundWindow.callback=lambda hwnd: 1
    assert w._restore_foreground(123) is True


def test_focus_restore_does_not_warn_when_target_already_has_focus(monkeypatch,capsys):
    import ctypes
    api=SimpleNamespace(SetForegroundWindow=NativeFunction(lambda hwnd: 0))
    monkeypatch.setattr(ctypes,'windll',SimpleNamespace(user32=api),raising=False)
    monkeypatch.setattr(w.sys,'platform','win32')
    monkeypatch.setattr(w,'_foreground_window',lambda:123)
    assert w._restore_foreground(123) is True
    assert capsys.readouterr().out == ''
