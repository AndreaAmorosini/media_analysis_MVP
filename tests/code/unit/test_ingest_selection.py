"""Selezione degli articoli per l'ingest (skip/limit).

Con un run precedente fatto con ``--limit 10``, ``--skip 10 --limit 20``
seleziona gli articoli 11-30 del dataset (ordinamento fisso): stesso
criterio del task ``pixi run e2e``.
"""

from types import SimpleNamespace

import pytest

import presskg.cli as cli


def make_cfg(tmp_path, count=30):
    for i in range(count):
        (tmp_path / f"article_{i:03d}.json").write_text("{}", encoding="utf-8")
    return SimpleNamespace(data_dir=tmp_path)


def test_select_article_paths_skip_and_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "CFG", make_cfg(tmp_path))
    selected = cli.select_article_paths(skip=10, limit=20)
    assert len(selected) == 20
    assert selected[0].name == "article_010.json"
    assert selected[-1].name == "article_029.json"


def test_select_article_paths_skip_zero_matches_limit_only(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "CFG", make_cfg(tmp_path))
    assert [p.name for p in cli.select_article_paths(limit=10)] == [
        f"article_{i:03d}.json" for i in range(10)
    ]


def test_select_article_paths_no_limit_takes_all(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "CFG", make_cfg(tmp_path, count=5))
    assert len(cli.select_article_paths(skip=0)) == 5


def test_select_article_paths_rejects_negative(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "CFG", make_cfg(tmp_path))
    with pytest.raises(ValueError):
        cli.select_article_paths(skip=-1)
    with pytest.raises(ValueError):
        cli.select_article_paths(limit=-1)
