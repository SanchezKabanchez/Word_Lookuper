import word_lookup as w


def test_network_presets_update_live_permissions(app):
    app._network_preset('deepl')
    assert not w.cfg['network']['offline']
    assert w.network_allowed('deepl')
    assert all(not w.network_allowed(name) for name in ('google', 'mymemory', 'datamuse', 'dictionaryapi'))
    app._network_preset('offline')
    assert w.cfg['network']['offline']
    assert not any(w.network_allowed(name) for name in w.cfg['network']['providers'])
    app._network_preset('all')
    assert all(w.network_allowed(name) for name in w.cfg['network']['providers'])


def test_clear_log_removes_current_and_rotated_files():
    from pathlib import Path
    w.log('private lookup')
    rotated = Path(w.log_path() + '.1')
    rotated.write_text('older private lookup', encoding='utf-8')
    w.clear_log()
    assert not Path(w.log_path()).exists()
    assert not rotated.exists()
    assert w.read_log_tail() == []


def test_copy_diagnostic_warning_can_cancel_without_reading_report(app, monkeypatch):
    prompts = []
    monkeypatch.setattr(w.messagebox, 'askyesno', lambda *args, **kwargs: prompts.append(args[1]) or False)
    def unexpected():
        raise AssertionError('Cancelled diagnostic must not access clipboard or report')
    monkeypatch.setattr(w, 'diagnostic_report', unexpected)
    monkeypatch.setattr(w.pyperclip, 'copy', lambda value: unexpected())
    app.copy_log()
    assert 'слова' in prompts[0]


def test_card_shows_corpus_frequency_without_cefr(app, card):
    app.show_lookup(card, None, (100, 100, 130, 130))
    assert app.popup.frequency_badge.cget('text') in ('Дуже часте', 'Часте', 'Поширене', 'Рідше')
    assert 'B2' not in app.popup.frequency_badge.cget('text')
