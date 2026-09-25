from rc import gha


def test_commands_escape_data(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    gha.notice("50% done\nnext", title="a:b,c")
    assert capsys.readouterr().out == "::notice title=a%3Ab%2Cc::50%25 done%0Anext\n"


def test_plain_output_outside_actions(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    gha.warning("careful")
    assert capsys.readouterr().err == "warning: careful\n"


def test_set_output_uses_a_delimiter(monkeypatch, tmp_path):
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    gha.set_output("matrix", '{"a": 1}\nsecond line')
    text = out.read_text()
    first, rest = text.split("\n", 1)
    assert first.startswith("matrix<<rc_")
    delimiter = first.split("<<", 1)[1]
    assert rest == '{"a": 1}\nsecond line\n' + delimiter + "\n"
