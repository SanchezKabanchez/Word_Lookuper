from types import SimpleNamespace

import word_lookup as w


def test_idle_poll_slows_only_when_no_interactive_work():
    state = SimpleNamespace(settings=w._settings, events=w.events, diagnostics=w._diagnostics,
                            instance=None, selector=None, free_cursor=None, popup=None, _game_pause=None, recording=None)
    assert w.App._pump_delay_ms(state) == 120
    for field in ('selector', 'free_cursor', 'popup', '_game_pause', 'recording'):
        setattr(state, field, object())
        assert w.App._pump_delay_ms(state) == 40
        setattr(state, field, None)
    w.events.put(('action', 'lookup_word'))
    assert w.App._pump_delay_ms(state) == 10


def test_event_burst_yields_back_to_tk_instead_of_draining_forever():
    seen, scheduled = [], []
    state = SimpleNamespace(settings=w._settings, events=w.events, diagnostics=w._diagnostics,
                            instance=None, selector=None, free_cursor=None, popup=None, _game_pause=None,
                            recording=None, _handle=seen.append,
                            root=SimpleNamespace(after=lambda delay, callback: scheduled.append(delay)),
                            _pump=lambda: None, _pump_delay_ms=lambda: 10)
    for i in range(500):
        w.events.put(('synthetic', i))
    w.App._pump(state)
    assert 0 < len(seen) <= 100
    assert w.events.qsize() == 500-len(seen)
    assert scheduled == [10]


def test_cpu_budget_controls_change_live_settings_without_enabling_pause(app):
    app._setting_vars[('ocr', 'cpu_threads')].set(1)
    app._setting_vars[('ocr', 'max_side')].set(1280)
    app._setting_vars[('performance', 'hover_delay_ms')].set(350)
    assert w.cfg['ocr']['cpu_threads'] == 1
    assert w.cfg['ocr']['max_side'] == 1280
    assert w.cfg['performance']['hover_delay_ms'] == 350
    assert not w.cfg['freeze']['pause_game']
    assert w.cfg['freeze']['pause_mode'] == 'lookup'


def test_v7_settings_gain_budgets_but_keep_saved_pause_limit(monkeypatch, tmp_path):
    import json
    from pathlib import Path
    Path(w.config_path()).write_text(json.dumps({'version':7,'freeze':{'pause_game':True,'pause_seconds':20}}))
    monkeypatch.setattr('word_lookup_secrets.load_key', lambda: '')
    loaded=w.load_config()
    assert loaded['freeze']['pause_game']
    assert loaded['freeze']['pause_seconds']==20
    assert loaded['freeze']['pause_mode']=='lookup'
    assert loaded['ocr']['cpu_threads']==2
    assert loaded['ocr']['max_side']==1600
