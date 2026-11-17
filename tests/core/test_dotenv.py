from argus.core.dotenv import load, parse


def test_parse_handles_comments_quotes_and_export():
    text = '# c\n\nA=1\nexport B="two words"\nC=\'x\'\nD=val # note\nE=\nnot a line\n'
    assert parse(text) == {"A": "1", "B": "two words", "C": "x", "D": "val", "E": ""}


def test_load_never_overrides_the_environment(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("ARGUS_T1=file\nARGUS_T2=file\nARGUS_T3=\n")
    monkeypatch.setenv("ARGUS_T1", "shell")
    monkeypatch.delenv("ARGUS_T2", raising=False)
    monkeypatch.delenv("ARGUS_T3", raising=False)
    assert load(f) == ["ARGUS_T2"]
    import os
    assert os.environ["ARGUS_T1"] == "shell" and os.environ["ARGUS_T2"] == "file"
    assert "ARGUS_T3" not in os.environ
    monkeypatch.delenv("ARGUS_T2")


def test_missing_file_is_fine(tmp_path):
    assert load(tmp_path / "nope") == []
