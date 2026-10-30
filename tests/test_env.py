import pickle
from types import SimpleNamespace

import pytest

from gcalcli import env
from gcalcli import gcal


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    """Point every data/config/legacy location into tmp_path."""
    home = tmp_path / 'home'
    home.mkdir()
    data_dir = tmp_path / 'data' / 'gcalcli'
    config_dir = tmp_path / 'config' / 'gcalcli'
    config_dir.mkdir(parents=True)
    xdg_cache_home = tmp_path / 'xdg_cache'
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.delenv('XDG_CACHE_HOME', raising=False)
    monkeypatch.setattr(env, 'default_data_dir', lambda: data_dir)
    return SimpleNamespace(
        home=home,
        data_dir=data_dir,
        config_dir=config_dir,
        xdg_cache_home=xdg_cache_home,
        xdg_cache=xdg_cache_home / 'gcalcli' / 'cache',
        legacy=home / '.gcalcli_cache',
    )


def test_cache_without_xdg_cache_home_keeps_data_file_location(dirs):
    assert env.cache_file_paths(dirs.config_dir)[0] == dirs.data_dir / 'cache'
    (dirs.config_dir / 'cache').touch()
    assert env.cache_file_paths(dirs.config_dir)[0] == (
        dirs.config_dir / 'cache')


@pytest.mark.parametrize('value', ['', 'relative/cache'])
def test_cache_ignores_empty_or_relative_xdg_cache_home(
        dirs, monkeypatch, value):
    monkeypatch.setenv('XDG_CACHE_HOME', value)
    assert env.cache_file_paths(dirs.config_dir)[0] == dirs.data_dir / 'cache'


def test_cache_with_xdg_cache_home_lists_old_files_after_it(
        dirs, monkeypatch):
    monkeypatch.setenv('XDG_CACHE_HOME', str(dirs.xdg_cache_home))
    (dirs.config_dir / 'cache').touch()
    dirs.legacy.touch()
    assert env.cache_file_paths(dirs.config_dir) == [
        dirs.xdg_cache,
        dirs.config_dir / 'cache',
        dirs.data_dir / 'cache',
        dirs.legacy,
    ]


def test_get_cached_reads_cache_from_xdg_cache_home(dirs, monkeypatch):
    monkeypatch.setenv('XDG_CACHE_HOME', str(dirs.xdg_cache_home))
    dirs.xdg_cache.parent.mkdir(parents=True)
    with dirs.xdg_cache.open('wb') as cache_file:
        pickle.dump({'all_cals': [{'id': 'from-xdg'}]}, cache_file)
    stub_self = SimpleNamespace(options={
        'config_folder': dirs.config_dir,
        'refresh_cache': False,
        'use_cache': True,
    })
    stub_self.cache_file_path = (
        lambda: env.cache_file_paths(dirs.config_dir)[0])
    gcal.GoogleCalendarInterface._get_cached(stub_self)
    assert stub_self.all_cals == [{'id': 'from-xdg'}]
